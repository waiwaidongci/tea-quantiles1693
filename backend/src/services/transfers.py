"""Export and import of frozen research between independent workspaces.

Import is a small state machine on the `transfers` row:

    (verify upload) -> pending  (local rows exist but are hidden)
                    -> ready    (database + archive + receipt + artifact written)
                    -> incomplete (a write failed; existing research kept, hidden)

Visibility is what makes the transition safe: imported studies, observations
and runs are created with visible=False and are only flipped together with the
transfer in the final commit, after the package archive and its receipt exist on
the shared volume.  A retry, a concurrent import or a resume of the same package
reuses the existing transfer and rows (research is never deleted).  A different
payload under an already-mapped source number is rejected instead of mixed in.
A packaged approval is evidence only; it is never turned into a local review.
"""
import fcntl
import hashlib
import json
import os
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.config.storage import artifact_root
from src.models.observation import Observation
from src.models.review import Review
from src.models.run import Run
from src.models.study import Study
from src.models.transfer import ResearchTransfer, StudySource, WorkspaceIdentity
from src.services import transfer_package as tp
from src.services.audits import log
from src.utils.serializers import checksum, now

PROCESS_LOCK=threading.Lock()


# ---------------------------------------------------------------- identity

def instance_id(db):
    """Stable identity of this working seat.

    WORKSPACE_INSTANCE_ID is authoritative when configured (each deployed
    workspace sets its own); otherwise the identity is generated once and
    persisted so exported packages always carry a stable source id.
    """
    value=os.environ.get('WORKSPACE_INSTANCE_ID','').strip()
    row=db.scalar(select(WorkspaceIdentity).order_by(WorkspaceIdentity.id).limit(1))
    if value:
        if row is None:
            db.add(WorkspaceIdentity(instance_id=value,created_at=now()))
            try: db.commit()
            except IntegrityError: db.rollback()
        return value
    if row: return row.instance_id
    generated='ws-'+uuid.uuid4().hex
    db.add(WorkspaceIdentity(instance_id=generated,created_at=now()))
    try:
        db.commit()
        return generated
    except IntegrityError:
        db.rollback()
        row=db.scalar(select(WorkspaceIdentity).order_by(WorkspaceIdentity.id).limit(1))
        if row: return row.instance_id
        raise


# ---------------------------------------------------------------- file helpers

def _sha_file(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
    return h.hexdigest()


def _atomic_write(path,payload):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.parent/f'.tmp-{uuid.uuid4().hex}'
    with open(tmp,'wb') as f:
        f.write(payload);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)
    try:
        dir_fd=os.open(path.parent,os.O_RDONLY)
        try: os.fsync(dir_fd)
        finally: os.close(dir_fd)
    except OSError:
        pass


@contextmanager
def _content_lock(fingerprint):
    try:
        lock_dir=artifact_root()/'.locks'
        lock_dir.mkdir(parents=True,exist_ok=True)
        handle=open(lock_dir/f'{fingerprint}.lock','a+')
    except OSError as exc:
        raise HTTPException(503,{'message':f'留档目录不可写：{exc}','report':{
            'ok':False,'checks':[],'failures':[],'summary':'留档目录不可写'}})
    with PROCESS_LOCK:
        try:
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(),fcntl.LOCK_UN);handle.close()


def _staging_path(package_id):
    return artifact_root()/'transfers'/'.staging'/f'{package_id}.zip'


def _archive_path(fingerprint,package_id):
    return artifact_root()/'transfers'/fingerprint[:16]/f'{package_id}.zip'


def _receipt_path(fingerprint,package_id):
    return artifact_root()/'transfers'/fingerprint[:16]/f'{package_id}.receipt.json'


def _run_artifact_path(run_id):
    return artifact_root()/f'run-{run_id}.json'


# ---------------------------------------------------------------- export

def export(db,run_id,user):
    run=db.get(Run,run_id)
    if not run or not run.visible: raise HTTPException(404,'运行不存在')
    if run.status!='completed' or not isinstance(run.result,dict): raise HTTPException(409,'只能打包已完成研究')
    study=db.get(Study,run.study_id)
    if not study or not study.visible: raise HTTPException(404,'业务批次不存在')
    review=db.scalar(select(Review).where(Review.run_id==run.id))
    artifact_bytes=None
    artifact=_run_artifact_path(run.id)
    if artifact.exists():
        try: parsed=json.loads(artifact.read_text())
        except json.JSONDecodeError: raise HTTPException(409,'结果附件损坏，已停止打包；请先核对留档')
        if tp.digest(parsed)!=tp.digest(run.result):
            raise HTTPException(409,'结果附件与数据库记录不一致，已停止打包；请先核对留档')
        artifact_bytes=artifact.read_bytes()
    raw,manifest=tp.build_package(study,run,review,artifact_bytes,instance_id(db),user.email,now())
    log(db,user,'run.package',f'{run.id}:{manifest["package_id"]}');db.commit()
    return raw,manifest


# ---------------------------------------------------------------- receive

def receive(db,raw,user):
    try: verified=tp.verify_package(raw)
    except tp.PackageError as exc:
        raise HTTPException(422,{'message':exc.message,'report':exc.report})
    manifest=verified['manifest']
    fingerprint=tp.digest(tp.package_fingerprint(manifest))
    package_id=verified['package_id']
    staging=_staging_path(package_id)
    # Stage the exact verified bytes before touching the database.  Recovery
    # can then finish from disk even if publishing the archive was interrupted.
    if not (staging.exists() and _sha_file(staging)==verified['package_sha256']):
        try: _atomic_write(staging,raw)
        except OSError as exc:
            raise HTTPException(503,{'message':f'无法写入暂存留档：{exc}','report':verified['report']})
    with _content_lock(fingerprint):
        transfer_id,created=_claim(db,verified,fingerprint,staging,user)
        result=_materialize(db,transfer_id,verified,raw,staging,user,'imported' if created else None)
        if result.get('status')=='ready' and staging.exists():
            try: staging.unlink()
            except OSError: pass
        return result


def _find_existing(db,package_id,source):
    transfer=db.scalar(select(ResearchTransfer).where(ResearchTransfer.package_id==package_id))
    if transfer: return transfer
    return db.scalar(select(ResearchTransfer).where(
        ResearchTransfer.source_instance_id==source['instance_id'],
        ResearchTransfer.source_study_id==source['study_id'],
        ResearchTransfer.source_run_id==source['run_id']))


def _conflict(message):
    return HTTPException(409,{'message':message,'report':{
        'ok':False,'checks':[],'failures':[],'summary':message}})


def _mapping_conflict(db,source,rel):
    mapping=db.scalar(select(StudySource).where(
        StudySource.source_instance_id==source['instance_id'],
        StudySource.source_study_id==source['study_id']))
    if mapping and mapping.study_payload_sha256!=rel['study_payload_sha256']:
        raise _conflict('同一来源批次编号已接收过不同内容，拒绝混合')
    return mapping


def _claim(db,verified,fingerprint,staging,user):
    manifest=verified['manifest'];source=manifest['source'];rel=manifest['relationships']
    package_id=verified['package_id']
    for _ in range(4):
        # Both identities must be checked against the same winner: a transfer
        # for this source run may have been claimed under another package id
        # by a concurrent upload with byte-different but identical content.
        existing=_find_existing(db,package_id,source)
        if existing:
            if existing.content_sha256!=fingerprint:
                raise _conflict('同一来源编号已存在不同内容的研究包，拒绝混合')
            return existing.id,False
        _mapping_conflict(db,source,rel)
        db.add(ResearchTransfer(
            package_id=package_id,package_sha256=verified['package_sha256'],content_sha256=fingerprint,
            source_instance_id=source['instance_id'],source_study_id=source['study_id'],
            source_run_id=source['run_id'],source_study_name=source['study_name'],
            source_run_name=source['run_name'],status='pending',manifest=manifest,
            validation=verified['report'],packaged_review=verified['review'],
            staging_path=str(staging),received_by=getattr(user,'email',str(user)),
            received_at=now(),updated_at=now()))
        try: db.commit()
        except IntegrityError:
            db.rollback();continue
        transfer=db.scalar(select(ResearchTransfer).where(ResearchTransfer.package_id==package_id))
        log(db,user,'transfer.receive',package_id)
        try: db.commit()
        except IntegrityError: db.rollback()
        return transfer.id,True
    raise _conflict('并发的同包导入未能收敛，请重试')


def _unique_study_name(db,base,source):
    base=(base or '').strip() or f"来源批次 {source['study_id']}"
    def taken(name): return db.scalar(select(Study.id).where(Study.name==name)) is not None
    if not taken(base[:120]): return base[:120]
    tag=f"迁移·{source['instance_id'][:8]}-{source['study_id']}"
    candidate=f'{base[:max(1,120-len(tag)-2)]}（{tag}）'
    if not taken(candidate): return candidate
    n=2
    while True:
        suffix=f'-{n}'
        candidate=f'{base[:max(1,120-len(tag)-len(suffix)-2)]}（{tag}{suffix}）'
        if not taken(candidate): return candidate
        n+=1


def _materialize(db,transfer_id,verified,raw,staging,user,outcome):
    """Create (or re-create) the hidden local rows for a claimed transfer.

    outcome is 'imported' for a fresh claim, 'recovered' for a retry/resume and
    None when the transfer already existed (a repeated upload)."""
    manifest=verified['manifest'];source=manifest['source'];rel=manifest['relationships']
    study_payload=verified['study'];rows=verified['observations']['rows'];run_payload=verified['run']
    effective='recovered' if outcome is None else outcome
    for attempt in range(3):
        transfer=db.get(ResearchTransfer,transfer_id)
        if transfer is None: raise HTTPException(500,'传输记录丢失')
        if transfer.status=='ready': return _summary(transfer,'duplicate',verified)
        try:
            mapping=_mapping_conflict(db,source,rel)
            study=db.get(Study,transfer.local_study_id) if transfer.local_study_id else None
            if study is None and mapping: study=db.get(Study,mapping.local_study_id)
            created_study=False
            if study is None:
                study=Study(name=_unique_study_name(db,study_payload['name'],source),
                            source_filename=study_payload['source_filename'],
                            description=study_payload['description'],visible=False)
                db.add(study);db.flush();created_study=True
            existing={x.source_key:x for x in db.scalars(
                select(Observation).where(Observation.study_id==study.id))}
            for row in rows:
                old=existing.get(row['source_key'])
                if old is None:
                    claim=Observation(study_id=study.id,source_key=row['source_key'],values=row['values'])
                    db.add(claim);db.flush();existing[row['source_key']]=claim
                elif old.values!=row['values']:
                    db.rollback()
                    raise _conflict(f"来源记录 {row['source_key']} 的内容与已接收批次不一致，拒绝混合")
            run=db.get(Run,transfer.local_run_id) if transfer.local_run_id else None
            if run is None:
                run=Run(study_id=study.id,name=run_payload['name'],status='completed',
                        parameters=run_payload['parameters'],snapshot=rows,
                        checksum=run_payload['checksum'],result=run_payload['result'],
                        error=run_payload.get('error',''),created_at=run_payload['created_at'],
                        visible=False)
                db.add(run);db.flush()
            if mapping is None:
                db.add(StudySource(source_instance_id=source['instance_id'],
                                   source_study_id=source['study_id'],
                                   study_payload_sha256=rel['study_payload_sha256'],
                                   local_study_id=study.id,created_at=now()))
            transfer.local_study_id=study.id;transfer.local_run_id=run.id
            transfer.status='pending';transfer.staging_path=str(staging);transfer.updated_at=now()
            db.commit()
            break
        except IntegrityError:
            db.rollback()
            transfer=db.get(ResearchTransfer,transfer_id)
            if transfer and transfer.status=='ready': return _summary(transfer,'duplicate',verified)
            if attempt==2: raise HTTPException(503,{
                'message':'本地编号冲突，导入未完成；可重试恢复',
                'transfer_id':transfer_id,'report':verified['report']})
    transfer=db.get(ResearchTransfer,transfer_id)
    return _publish(db,transfer,verified,raw,staging,user,effective,created_study)


def _publish(db,transfer,verified,raw,staging,user,outcome,created_study):
    fingerprint=transfer.content_sha256;package_id=transfer.package_id
    archive=Path(transfer.archive_path) if transfer.archive_path else _archive_path(fingerprint,package_id)
    receipt=_receipt_path(fingerprint,package_id)
    artifact=_run_artifact_path(transfer.local_run_id)
    try:
        if not (archive.exists() and _sha_file(archive)==transfer.package_sha256):
            archive.parent.mkdir(parents=True,exist_ok=True)
            if staging and Path(staging).exists() and _sha_file(staging)==transfer.package_sha256:
                os.replace(staging,archive)
            elif raw is not None:
                _atomic_write(archive,raw)
            else:
                _atomic_write(archive,_read_package_bytes(transfer))
        if _sha_file(archive)!=transfer.package_sha256:
            raise OSError('留档哈希与来源包不一致')
        receipt_body={'package_id':package_id,'package_sha256':transfer.package_sha256,
                      'content_sha256':fingerprint,'source':verified['manifest']['source'],
                      'relationships':verified['manifest']['relationships'],
                      'items':verified['manifest']['items'],
                      'source_review':transfer.packaged_review,'review_is_local':False,
                      'local':{'study_id':transfer.local_study_id,'run_id':transfer.local_run_id},
                      'received_by':transfer.received_by,'received_at':transfer.received_at,
                      'verified_at':now(),'validation':verified['report']}
        _atomic_write(receipt,json.dumps(receipt_body,ensure_ascii=False,allow_nan=False).encode('utf-8'))
        _atomic_write(artifact,tp.canonical(verified['run']['result']).encode('utf-8'))
    except (OSError,ValueError) as exc:
        db.rollback()
        transfer=db.get(ResearchTransfer,transfer.id)
        if archive.exists() and _sha_file(archive)==transfer.package_sha256:
            transfer.archive_path=str(archive)
        transfer.status='incomplete';transfer.error=f'留档写入失败：{exc}';transfer.updated_at=now()
        log(db,user,'transfer.incomplete',f'{transfer.id}:{exc}');db.commit()
        raise HTTPException(503,{
            'message':f'研究已入库但留档未完成，暂不可见；可重试恢复（传输编号 {transfer.id}）',
            'transfer_id':transfer.id,'report':verified['report']})
    study=db.get(Study,transfer.local_study_id);run=db.get(Run,transfer.local_run_id)
    transfer.archive_path=str(archive);transfer.archive_sha256=transfer.package_sha256
    transfer.status='ready';transfer.validation=verified['report'];transfer.error=''
    transfer.verified_at=now();transfer.updated_at=now()
    study.visible=True;run.visible=True
    log(db,user,'transfer.ready',f'{transfer.id}:{package_id}: study {study.id} run {run.id}'
        + ('（新建批次）' if created_study else '（复用既有批次）'))
    try: db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(503,{'message':'数据库提交中断，研究暂不可见；可重试恢复',
                                 'transfer_id':transfer.id,'report':verified['report']})
    try:
        if staging and Path(staging).exists(): Path(staging).unlink()
    except OSError:
        pass
    return _summary(db.get(ResearchTransfer,transfer.id),outcome,verified)


def _read_package_bytes(transfer):
    for candidate in (transfer.staging_path and Path(transfer.staging_path),
                      transfer.archive_path and Path(transfer.archive_path)):
        if candidate and candidate.exists():
            data=candidate.read_bytes()
            if hashlib.sha256(data).hexdigest()==transfer.package_sha256: return data
    raise OSError('找不到可用的来源包字节，请重新上传原研究包')


# ---------------------------------------------------------------- views

def _summary(transfer,outcome,verified=None):
    report=(verified or {}).get('report') or transfer.validation or {}
    return {'id':transfer.id,'outcome':outcome,'status':transfer.status,
            'package_id':transfer.package_id,'package_sha256':transfer.package_sha256,
            'content_sha256':transfer.content_sha256,
            'source':{'instance_id':transfer.source_instance_id,'study_id':transfer.source_study_id,
                      'study_name':transfer.source_study_name,'run_id':transfer.source_run_id,
                      'run_name':transfer.source_run_name},
            'local':{'study_id':transfer.local_study_id,'run_id':transfer.local_run_id},
            'source_review':transfer.packaged_review,'review_is_local':False,
            'verification':{'ok':bool(report.get('ok')),'summary':report.get('summary',''),
                            'checks':len(report.get('checks',[])) if isinstance(report,dict) else 0},
            'error':transfer.error,'received_by':transfer.received_by,
            'received_at':transfer.received_at,'verified_at':transfer.verified_at}


def _detail(transfer):
    data=_summary(transfer,'detail')
    data.update({'manifest':transfer.manifest,'validation':transfer.validation,
                 'archive_path':transfer.archive_path,'archive_sha256':transfer.archive_sha256,
                 'archive_verified':None})
    if transfer.status=='ready' and transfer.archive_path and Path(transfer.archive_path).exists():
        data['archive_verified']=_sha_file(transfer.archive_path)==transfer.package_sha256
    elif transfer.status=='ready':
        data['archive_verified']=False
    return data


def list_transfers(db):
    return [_summary(x,'list') for x in db.scalars(
        select(ResearchTransfer).order_by(ResearchTransfer.id.desc()))]


def get(db,transfer_id):
    transfer=db.get(ResearchTransfer,transfer_id)
    if not transfer: raise HTTPException(404,'传输记录不存在')
    return _detail(transfer)


# ---------------------------------------------------------------- verify/resume

def _verify_transfer(db,transfer):
    failures=[]
    def fail(item,detail): failures.append({'item':item,'check':'integrity','detail':detail})
    archive=Path(transfer.archive_path) if transfer.archive_path else None
    if not archive or not archive.exists(): fail('archive','留档文件缺失')
    elif _sha_file(archive)!=transfer.package_sha256: fail('archive','留档文件哈希与接收记录不一致')
    receipt=_receipt_path(transfer.content_sha256,transfer.package_id)
    if not receipt.exists(): fail('receipt','接收凭据缺失，留档未完整落妥')
    else:
        try:
            evidence=json.loads(receipt.read_text())
            if evidence.get('package_sha256')!=transfer.package_sha256: fail('receipt','接收凭据哈希不一致')
            if evidence.get('content_sha256')!=transfer.content_sha256: fail('receipt','接收凭据内容编号不一致')
            if (evidence.get('local') or {}).get('run_id')!=transfer.local_run_id: fail('receipt','接收凭据关联的本地运行不一致')
        except (json.JSONDecodeError,OSError,AttributeError): fail('receipt','接收凭据损坏')
    verified=None
    if archive and archive.exists():
        try: verified=tp.verify_package(archive.read_bytes())
        except tp.PackageError as exc: fail('archive',exc.message)
    if verified:
        if tp.digest(tp.package_fingerprint(verified['manifest']))!=transfer.content_sha256:
            fail('archive','留档内容与接收记录不一致')
        study=db.get(Study,transfer.local_study_id);run=db.get(Run,transfer.local_run_id)
        if not study or not run: fail('database','本地研究记录缺失')
        else:
            rel=verified['manifest']['relationships']
            if study.source_filename!=verified['study']['source_filename']: fail('study','来源文件与留档不一致')
            if study.description!=(verified['study']['description'] or ''): fail('study','批次说明与留档不一致')
            if run.status!='completed': fail('run','本地运行状态不是已完成')
            if run.checksum!=rel['snapshot_sha256']: fail('run','运行快照哈希与留档不一致')
            if tp.digest(run.parameters)!=rel['parameters_sha256']: fail('run','参数与留档不一致')
            if tp.digest(run.result)!=rel['result_sha256']: fail('run','结果与留档不一致')
            rows=[{'source_key':x['source_key'],'values':x['values']} for x in run.snapshot] if isinstance(run.snapshot,list) else []
            if checksum(rows)!=rel['snapshot_sha256']: fail('run','冻结观测与留档不一致')
            local={x.source_key:x.values for x in db.scalars(
                select(Observation).where(Observation.study_id==study.id))}
            frozen={x['source_key']:x['values'] for x in verified['observations']['rows']}
            # Every frozen row must still be present unchanged.  Rows added
            # locally after import are allowed: they are not package content.
            if any(local.get(key)!=values for key,values in frozen.items()):
                fail('observations','本地观测行与冻结观测不一致')
            artifact=_run_artifact_path(run.id)
            if not artifact.exists(): fail('artifact','结果附件缺失')
            else:
                try:
                    if tp.digest(json.loads(artifact.read_text()))!=tp.digest(run.result):
                        fail('artifact','结果附件与记录不一致')
                except json.JSONDecodeError: fail('artifact','结果附件损坏')
    ok=not failures
    study=db.get(Study,transfer.local_study_id);run=db.get(Run,transfer.local_run_id)
    if not ok:
        transfer.status='incomplete';transfer.error='复核未通过：'+failures[0]['detail']
        if study: study.visible=False
        if run: run.visible=False
    else:
        transfer.status='ready';transfer.error=''
        if study: study.visible=True
        if run: run.visible=True
    transfer.verified_at=now();transfer.updated_at=now()
    db.commit()
    return {'ok':ok,'summary':'复核通过：内容、关联与复算一致' if ok else failures[0]['detail'],
            'failures':failures,'archive_verified':ok}


def verify(db,transfer_id,user):
    transfer=db.get(ResearchTransfer,transfer_id)
    if not transfer: raise HTTPException(404,'传输记录不存在')
    result=_verify_transfer(db,transfer)
    log(db,user,'transfer.verify',f'{transfer_id}:{result["ok"]}:{result["summary"]}');db.commit()
    data=_summary(db.get(ResearchTransfer,transfer_id),'verify')
    data['verification']=result
    return data


def resume(db,transfer_id,user):
    transfer=db.get(ResearchTransfer,transfer_id)
    if not transfer: raise HTTPException(404,'传输记录不存在')
    if transfer.status=='ready': return _summary(transfer,'duplicate')
    raw=None
    for candidate in (transfer.staging_path and Path(transfer.staging_path),
                      transfer.archive_path and Path(transfer.archive_path),
                      _archive_path(transfer.content_sha256,transfer.package_id)):
        if candidate and candidate.exists():
            data=candidate.read_bytes()
            if hashlib.sha256(data).hexdigest()==transfer.package_sha256:
                raw=data;break
    if raw is None:
        raise HTTPException(409,{'message':'留档或暂存已缺失，无法自动恢复；请重新上传原研究包',
                                 'transfer_id':transfer.id})
    try: verified=tp.verify_package(raw)
    except tp.PackageError as exc:
        raise HTTPException(422,{'message':exc.message,'report':exc.report})
    if tp.digest(tp.package_fingerprint(verified['manifest']))!=transfer.content_sha256:
        raise _conflict('留档内容与接收记录不一致，拒绝恢复')
    with _content_lock(transfer.content_sha256):
        transfer=db.get(ResearchTransfer,transfer_id)
        if transfer.status=='ready': return _summary(transfer,'duplicate')
        staging=Path(transfer.staging_path) if transfer.staging_path else None
        if transfer.local_study_id is None or transfer.local_run_id is None:
            result=_materialize(db,transfer.id,verified,raw,staging,user,'recovered')
        else:
            result=_publish(db,transfer,verified,raw,staging,user,'recovered',False)
        log(db,user,'transfer.resume',str(transfer.id));db.commit()
        return result
