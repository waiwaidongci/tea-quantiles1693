"""Portable, self-verifying research packages.

A package is a ZIP file containing a manifest and immutable research items:
the frozen observations used for a run, the run parameters, the recorded
result, the shared result artifact and, when present, the release-note
evidence.  Verification is item-by-item: every declared file must exist,
match its size and SHA256, parse into the expected schema, agree with the
declared relationships, and reproduce the recorded result from the frozen
observations and parameters.

Any missing, extra or altered item rejects the whole package.  Verification
never writes to the database or the shared archive.
"""
import hashlib
import io
import json
import math
import zipfile
from pathlib import PurePosixPath

from src.services.algorithm import calculate
from src.services.domain_validation import validate_parameters
from src.utils.serializers import checksum

FORMAT='tea-quantiles-research-package'
VERSION=1
MANIFEST='manifest.json'
REQUIRED_ROLES=('study','observations','run','result')
OPTIONAL_ROLES=('review',)
ALLOWED_ROLES=REQUIRED_ROLES+OPTIONAL_ROLES
MAX_ZIP_BYTES=25*1024*1024
MAX_ENTRY_BYTES=10*1024*1024
MAX_TOTAL_UNCOMPRESSED=50*1024*1024
MAX_ENTRIES=40
MAX_REPORT_CHECKS=400


def canonical(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def study_payload_digest(study_payload):
    """Identity of a source study's metadata.  Observations are frozen per run
    and deliberately excluded: several runs may be exported from one study."""
    return digest(study_payload)


def package_fingerprint(manifest):
    """Content identity used to decide whether a repeated source number is a
    duplicate or a content conflict.

    Only semantic digests count: re-exporting the same run is a duplicate even
    if the shared result artifact was re-serialised with different whitespace.
    Timestamps and exporter identity are excluded."""
    rel=manifest['relationships']
    return {
        'study_payload_sha256':rel['study_payload_sha256'],
        'snapshot_sha256':rel['snapshot_sha256'],
        'parameters_sha256':rel['parameters_sha256'],
        'result_sha256':rel['result_sha256'],
        'review_sha256':rel.get('review_sha256'),
    }


class PackageError(Exception):
    def __init__(self,message,report=None):
        super().__init__(message)
        self.message=message
        self.report=report or {'ok':False,'checks':[],'failures':[],'summary':message}


class _Checks:
    def __init__(self):
        self.entries=[]
        self.failures=[]
        self.truncated=False

    def add(self,section,item,check,ok,detail=''):
        ok=bool(ok)
        if len(self.entries)<MAX_REPORT_CHECKS:
            self.entries.append({'section':section,'item':item,'check':check,'ok':ok,'detail':'' if ok else detail})
        else:
            self.truncated=True
        if not ok:
            self.failures.append({'section':section,'item':item,'check':check,'ok':False,'detail':detail})

    def expect(self,condition,section,item,check,detail=''):
        self.add(section,item,check,bool(condition),'' if condition else detail)
        return bool(condition)

    def report(self,package_id=None,package_sha256=None):
        summary='全部内容与关联校验通过'
        if self.failures:
            first=self.failures[0]
            summary=f"{first['item']} {first['check']} 校验失败" + (f"：{first['detail']}" if first['detail'] else '')
            if len(self.failures)>1: summary+=f'（共{len(self.failures)}项不通过）'
        return {'ok':not self.failures,'package_id':package_id,'package_sha256':package_sha256,
                'checks':self.entries,'failures':self.failures,'summary':summary,'truncated':self.truncated}


def _safe_name(name):
    if not isinstance(name,str) or not name or '\\' in name or name.startswith('/'): return False
    parts=PurePosixPath(name).parts
    return bool(parts) and all(p not in ('','.','..') for p in parts)


def _load_zip(raw,chk):
    if not raw:
        raise PackageError('研究包为空',chk.report())
    if len(raw)>MAX_ZIP_BYTES:
        raise PackageError('研究包超过25MB上限',chk.report())
    try:
        zf=zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile,OSError,ValueError) as exc:
        raise PackageError(f'研究包不是有效的ZIP文件：{exc}',chk.report())
    infos=zf.infolist()
    if len(infos)>MAX_ENTRIES:
        raise PackageError(f'研究包条目超过{MAX_ENTRIES}个上限',chk.report())
    names=[x.filename for x in infos]
    total=0
    ok=True
    for info in infos:
        name=info.filename
        if info.is_dir():
            ok=chk.expect(False,'package',name,'regular-file','不允许目录条目') and ok
            continue
        if not _safe_name(name):
            ok=chk.expect(False,'package',name,'safe-path','路径不安全') and ok
        if info.flag_bits & 0x1:
            ok=chk.expect(False,'package',name,'unencrypted','不允许加密条目') and ok
        if info.file_size>MAX_ENTRY_BYTES:
            ok=chk.expect(False,'package',name,'entry-size','单个条目超过10MB') and ok
        total+=max(0,info.file_size)
        if info.compress_size and info.file_size>1024*1024 and info.file_size/max(1,info.compress_size)>200:
            ok=chk.expect(False,'package',name,'compression-ratio','压缩比异常') and ok
    chk.expect(ok,'package',MANIFEST,'zip-structure','ZIP结构不安全')
    chk.expect(total<=MAX_TOTAL_UNCOMPRESSED,'package',MANIFEST,'total-size',f'解压总量{total}超过50MB')
    if len(set(names))!=len(names):
        chk.expect(False,'package',MANIFEST,'unique-names','ZIP存在重名条目')
    chk.expect(MANIFEST in names,'package',MANIFEST,'present','缺少清单文件')
    if chk.failures: raise PackageError('研究包结构校验未通过',chk.report())
    return zf,names


def _read_entry(zf,name,chk):
    try:
        with zf.open(name) as handle:
            payload=handle.read(MAX_ENTRY_BYTES+1)
        if len(payload)>MAX_ENTRY_BYTES:
            chk.add('item',name,'entry-size',False,'条目解压后超过10MB上限')
            return None
        return payload
    except (zipfile.BadZipFile,OSError,RuntimeError,ValueError) as exc:
        chk.add('item',name,'read',False,f'条目无法读取（压缩数据损坏）：{exc}')
        return None


def _parse_json(payload,item,chk):
    if payload is None: return None
    try:
        return json.loads(payload.decode('utf-8'))
    except (UnicodeDecodeError,json.JSONDecodeError) as exc:
        chk.add('item',item,'json',False,f'不是有效JSON：{exc}')
        return None


def _manifest_shape(manifest,chk):
    if not isinstance(manifest,dict):
        chk.expect(False,'manifest',MANIFEST,'object','清单必须是对象')
        return False
    allowed={'format','version','package_id','created_at','exporter','source','relationships','items'}
    ok=chk.expect(set(manifest)==allowed,'manifest',MANIFEST,'keys','清单字段缺失或多余')
    ok=chk.expect(manifest.get('format')==FORMAT,'manifest',MANIFEST,'format',f"格式必须为{FORMAT}") and ok
    ok=chk.expect(manifest.get('version')==VERSION,'manifest',MANIFEST,'version',f'仅支持版本{VERSION}') and ok
    ok=chk.expect(isinstance(manifest.get('package_id'),str) and manifest.get('package_id'),'manifest',MANIFEST,'package-id','缺少包编号') and ok
    ok=chk.expect(isinstance(manifest.get('created_at'),str) and manifest.get('created_at'),'manifest',MANIFEST,'created-at','缺少打包时间') and ok
    source=manifest.get('source'); exporter=manifest.get('exporter'); rel=manifest.get('relationships'); items=manifest.get('items')
    ok=chk.expect(isinstance(source,dict) and set(source)=={'instance_id','study_id','study_name','run_id','run_name'},'manifest',MANIFEST,'source','来源字段缺失或多余') and ok
    ok=chk.expect(isinstance(exporter,dict) and set(exporter)=={'instance_id','email'},'manifest',MANIFEST,'exporter','打包者字段缺失或多余') and ok
    rel_keys={'study_id','run_study_id','observations_study_id','observations_count','snapshot_sha256','parameters_sha256','result_sha256','study_payload_sha256','review_run_id','review_sha256'}
    ok=chk.expect(isinstance(rel,dict) and set(rel)==rel_keys,'manifest',MANIFEST,'relationships','关联字段缺失或多余') and ok
    ok=chk.expect(isinstance(items,list) and items,'manifest',MANIFEST,'items','缺少条目列表') and ok
    if isinstance(items,list):
        for n,item in enumerate(items):
            label=f'items[{n}]'
            if not isinstance(item,dict) or set(item)!={'path','role','sha256','size'}:
                ok=chk.expect(False,'manifest',label,'item-shape','条目字段缺失或多余') and ok
                continue
            ok=chk.expect(item.get('role') in ALLOWED_ROLES,'manifest',label,'role','未知条目角色') and ok
            ok=chk.expect(_safe_name(item.get('path')),'manifest',label,'path','条目路径不安全') and ok
            ok=chk.expect(isinstance(item.get('sha256'),str) and len(item.get('sha256'))==64,'manifest',label,'sha256','条目哈希格式错误') and ok
            ok=chk.expect(isinstance(item.get('size'),int) and 0<=item.get('size')<=MAX_ENTRY_BYTES,'manifest',label,'size','条目大小错误') and ok
    if isinstance(source,dict):
        ok=chk.expect(all(isinstance(source.get(k),str) and source.get(k) for k in ('instance_id','study_id','study_name','run_id','run_name')),'manifest',MANIFEST,'source-values','来源字段必须为非空字符串') and ok
    if isinstance(exporter,dict):
        ok=chk.expect(isinstance(exporter.get('instance_id'),str) and exporter.get('instance_id'),'manifest',MANIFEST,'exporter-instance','打包者实例号缺失') and ok
        ok=chk.expect(isinstance(exporter.get('email'),str) and exporter.get('email'),'manifest',MANIFEST,'exporter-email','打包者账号缺失') and ok
    return ok


def _item_schemas(item,data,chk):
    role=item['role']; path=item['path']; ok=True
    if role=='study':
        ok=chk.expect(isinstance(data,dict) and set(data)=={'id','name','source_filename','description'},'item',path,'schema','study字段缺失或多余') and ok
        if isinstance(data,dict):
            ok=chk.expect(isinstance(data.get('id'),str) and data.get('id'),'item',path,'id','study编号缺失') and ok
            ok=chk.expect(isinstance(data.get('name'),str) and 1<=len(data.get('name',''))<=120,'item',path,'name','study名称长度错误') and ok
            ok=chk.expect(isinstance(data.get('source_filename'),str) and 1<=len(data.get('source_filename',''))<=120,'item',path,'source-filename','来源文件名错误') and ok
            ok=chk.expect(isinstance(data.get('description',''),str) and len(data.get('description',''))<=2000,'item',path,'description','说明长度错误') and ok
    elif role=='observations':
        ok=chk.expect(isinstance(data,dict) and set(data)=={'study_id','count','checksum','rows'},'item',path,'schema','observations字段缺失或多余') and ok
        if isinstance(data,dict):
            rows=data.get('rows')
            ok=chk.expect(isinstance(data.get('study_id'),str) and data.get('study_id'),'item',path,'study-id','observations缺少study编号') and ok
            ok=chk.expect(isinstance(rows,list) and 0<len(rows)<=5000,'item',path,'rows','冻结观测必须为1到5000条') and ok
            if isinstance(rows,list):
                good=True; keys=[]; finite=True
                for n,row in enumerate(rows):
                    if not isinstance(row,dict) or set(row)!={'source_key','values'}:
                        good=False; continue
                    key=row.get('source_key'); values=row.get('values')
                    if not isinstance(key,str) or not 1<=len(key)<=100 or not isinstance(values,dict):
                        good=False; continue
                    keys.append(key)
                    for v in values.values():
                        if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(float(v)):
                            finite=False
                ok=chk.expect(good,'item',path,'row-schema','观测行结构错误') and ok
                ok=chk.expect(len(keys)==len(set(keys)),'item',path,'row-unique','源记录编号重复') and ok
                ok=chk.expect(finite,'item',path,'row-finite','观测必须是有限数值') and ok
            ok=chk.expect(isinstance(data.get('count'),int) and data.get('count')==len(rows or []),'item',path,'count','观测条数与清单不一致') and ok
            ok=chk.expect(isinstance(data.get('checksum'),str) and len(data.get('checksum',''))==64,'item',path,'checksum','观测快照哈希格式错误') and ok
    elif role=='run':
        ok=chk.expect(isinstance(data,dict) and set(data)=={'id','study_id','name','status','parameters','checksum','result','error','created_at'},'item',path,'schema','run字段缺失或多余') and ok
        if isinstance(data,dict):
            ok=chk.expect(isinstance(data.get('id'),str) and data.get('id'),'item',path,'id','run编号缺失') and ok
            ok=chk.expect(isinstance(data.get('study_id'),str) and data.get('study_id'),'item',path,'study-id','run缺少study编号') and ok
            ok=chk.expect(isinstance(data.get('name'),str) and 1<=len(data.get('name',''))<=120,'item',path,'name','run名称长度错误') and ok
            ok=chk.expect(data.get('status')=='completed','item',path,'status','只迁移已完成研究') and ok
            ok=chk.expect(isinstance(data.get('parameters'),dict),'item',path,'parameters','参数必须为对象') and ok
            ok=chk.expect(isinstance(data.get('checksum'),str) and len(data.get('checksum',''))==64,'item',path,'checksum','运行哈希格式错误') and ok
            ok=chk.expect(isinstance(data.get('result'),dict),'item',path,'result','结果必须为对象') and ok
            ok=chk.expect(isinstance(data.get('error',''),str),'item',path,'error','错误字段必须为字符串') and ok
            ok=chk.expect(isinstance(data.get('created_at'),str) and data.get('created_at'),'item',path,'created-at','运行时间缺失') and ok
    elif role=='review':
        ok=chk.expect(isinstance(data,dict) and set(data)=={'run_id','note','author','created_at'},'item',path,'schema','review字段缺失或多余') and ok
        if isinstance(data,dict):
            ok=chk.expect(isinstance(data.get('run_id'),str) and data.get('run_id'),'item',path,'run-id','review缺少run编号') and ok
            ok=chk.expect(isinstance(data.get('note'),str) and 5<=len(data.get('note',''))<=2000,'item',path,'note','复核意见长度错误') and ok
            ok=chk.expect(isinstance(data.get('author'),str) and data.get('author'),'item',path,'author','复核人缺失') and ok
            ok=chk.expect(isinstance(data.get('created_at'),str) and data.get('created_at'),'item',path,'created-at','复核时间缺失') and ok
    elif role=='result':
        ok=chk.expect(isinstance(data,dict),'item',path,'schema','结果附件必须为JSON对象') and ok
    return ok


def verify_package(raw):
    """Verify an uploaded package.  Raises PackageError for a whole-package
    rejection and returns parsed payloads on success."""
    chk=_Checks()
    zf,names=_load_zip(raw,chk)
    # Read the manifest first.
    manifest_info=zf.getinfo(MANIFEST)
    if manifest_info.file_size>MAX_ENTRY_BYTES:
        raise PackageError('清单文件过大',chk.report())
    manifest=_parse_json(_read_entry(zf,MANIFEST,chk),MANIFEST,chk)
    if manifest is None or not _manifest_shape(manifest,chk):
        report=chk.report()
        raise PackageError(report['summary'],report)
    body=dict(manifest); declared_id=body.pop('package_id')
    computed_id=digest(body)
    chk.expect(declared_id==computed_id,'manifest',MANIFEST,'package-id-hash','包编号与清单内容不一致')
    package_id=declared_id
    # Declared files must exactly match ZIP contents.
    items=manifest['items']
    paths=[x['path'] for x in items]
    roles=[x['role'] for x in items]
    chk.expect(len(paths)==len(set(paths)),'manifest',MANIFEST,'unique-paths','条目路径重复')
    chk.expect(len(roles)==len(set(roles)),'manifest',MANIFEST,'unique-roles','条目角色重复')
    for role in REQUIRED_ROLES:
        chk.expect(role in roles,'manifest',MANIFEST,f'required-{role}',f'缺少必需条目：{role}')
    rel=manifest['relationships']
    has_review=rel.get('review_run_id') is not None
    chk.expect(('review' in roles)==has_review,'manifest',MANIFEST,'review-presence','复核条目与关联声明不一致')
    expected=set(paths)|{MANIFEST}
    actual=set(names)
    chk.expect(actual==expected,'package',MANIFEST,'exact-files',
               f"缺件：{sorted(expected-actual)}；多余：{sorted(actual-expected)}")
    for item in items:
        if item['role']=='result':
            chk.expect(item['path'].startswith('files/'),'manifest',item['path'],'role-path','结果附件应位于files/')
        else:
            chk.expect(item['path'].startswith('items/'),'manifest',item['path'],'role-path','研究条目应位于items/')
    parsed={}
    for item in items:
        path=item['path']; role=item['role']
        if path not in actual:
            continue
        payload=_read_entry(zf,path,chk)
        if payload is None: continue
        chk.expect(len(payload)==item['size'],'item',path,'size',f"声明{item['size']}字节，实际{len(payload)}字节")
        actual_hash=sha256_bytes(payload)
        chk.expect(actual_hash==item['sha256'],'item',path,'sha256','内容哈希与清单不一致，疑似篡改')
        data=_parse_json(payload,path,chk)
        if data is None:
            chk.expect(False,'item',path,'content','条目内容无法解析')
            continue
        _item_schemas(item,data,chk)
        parsed[role]=data
    # Relationships and cross-item agreement.
    source=manifest['source']
    study=parsed.get('study'); observations=parsed.get('observations'); run=parsed.get('run')
    review=parsed.get('review'); artifact=parsed.get('result')
    if isinstance(study,dict):
        chk.expect(study.get('id')==source['study_id'],'relationship','study','source-id','study编号与来源不一致')
        chk.expect(study.get('name')==source['study_name'],'relationship','study','source-name','study名称与来源不一致')
        chk.expect(rel.get('study_id')==source['study_id'],'relationship','study','declared-id','study关联声明不一致')
    if isinstance(run,dict):
        chk.expect(run.get('id')==source['run_id'],'relationship','run','source-id','run编号与来源不一致')
        chk.expect(run.get('name')==source['run_name'],'relationship','run','source-name','run名称与来源不一致')
        chk.expect(run.get('study_id')==source['study_id'],'relationship','run','study-id','run关联的study编号不一致')
        chk.expect(rel.get('run_study_id')==run.get('study_id'),'relationship','run','declared-study','run关联声明不一致')
    if isinstance(observations,dict):
        chk.expect(observations.get('study_id')==source['study_id'],'relationship','observations','study-id','观测关联的study编号不一致')
        chk.expect(rel.get('observations_study_id')==observations.get('study_id'),'relationship','observations','declared-study','观测关联声明不一致')
        chk.expect(rel.get('observations_count')==observations.get('count'),'relationship','observations','count','观测条数关联不一致')
        rows=observations.get('rows')
        if isinstance(rows,list):
            try: row_sum=checksum(rows)
            except ValueError: row_sum=None
            chk.expect(observations.get('checksum')==row_sum,'relationship','observations','checksum','冻结观测哈希与内容不一致')
            chk.expect(rel.get('snapshot_sha256')==row_sum,'relationship','observations','snapshot','快照关联哈希不一致')
            if isinstance(run,dict): chk.expect(run.get('checksum')==row_sum,'relationship','observations','run-checksum','运行哈希与冻结观测不一致')
            if isinstance(study,dict):
                try: payload_sum=study_payload_digest(study)
                except ValueError: payload_sum=None
                chk.expect(rel.get('study_payload_sha256')==payload_sum,'relationship','study','payload','study内容关联哈希不一致')
    if isinstance(run,dict):
        try: params_sum=digest(run.get('parameters'))
        except (ValueError,TypeError): params_sum=None
        chk.expect(rel.get('parameters_sha256')==params_sum,'relationship','run','parameters-hash','参数关联哈希不一致')
        try: result_sum=digest(run.get('result'))
        except (ValueError,TypeError): result_sum=None
        chk.expect(rel.get('result_sha256')==result_sum,'relationship','run','result-hash','结果关联哈希不一致')
        try:
            validate_parameters(run.get('parameters') or {})
            chk.add('relationship','run','parameters-valid',True,'')
        except (ValueError,TypeError) as exc:
            chk.add('relationship','run','parameters-valid',False,str(exc))
        if isinstance(run.get('result'),dict):
            chk.expect(run['result'].get('input_sha256') in (None,run.get('checksum')),'relationship','run','input-sha','结果记录的输入哈希不一致')
    if has_review:
        if isinstance(review,dict):
            chk.expect(review.get('run_id')==source['run_id'],'relationship','review','run-id','复核关联的run编号不一致')
            chk.expect(rel.get('review_run_id')==review.get('run_id'),'relationship','review','declared-run','复核关联声明不一致')
            try: review_sum=digest(review)
            except (ValueError,TypeError): review_sum=None
            chk.expect(rel.get('review_sha256')==review_sum,'relationship','review','review-hash','复核依据哈希不一致')
    else:
        chk.expect(rel.get('review_sha256') is None,'relationship','review','review-hash','无复核时不应有复核哈希')
    if isinstance(run,dict) and isinstance(artifact,dict):
        try: same=digest(run.get('result'))==digest(artifact)
        except (ValueError,TypeError): same=False
        chk.expect(same,'relationship','result','artifact-equals-result','结果附件与记录结果不一致')
    report=chk.report(package_id=package_id,package_sha256=sha256_bytes(raw))
    if chk.failures:
        raise PackageError(report['summary'],report)
    # Full recomputation is the strongest check: result must be reproducible
    # from the frozen observations and parameters.
    try:
        expected=calculate(observations['rows'],run['parameters'])
        if run['checksum']: expected['input_sha256']=run['checksum']
        same=digest(expected)==digest(run['result'])
        detail='' if same else '结果不能由冻结观测与参数复算得出，疑似篡改'
    except Exception as exc:
        same=False; detail=f'复算失败：{exc}'
    chk.add('recompute','run','result-reproducible',bool(same),detail)
    report=chk.report(package_id=package_id,package_sha256=sha256_bytes(raw))
    if chk.failures:
        raise PackageError(report['summary'],report)
    return {'manifest':manifest,'study':study,'observations':observations,'run':run,
            'review':review,'artifact':artifact,'report':report,
            'package_id':package_id,'package_sha256':sha256_bytes(raw)}


def _zip_entry(name,payload):
    info=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0))
    info.compress_type=zipfile.ZIP_DEFLATED
    info.external_attr=0o644<<16
    return info,payload


def build_package(study,run,review,artifact_bytes,instance_id,exporter_email,created_at):
    """Build a package from database rows.  `artifact_bytes` is the shared
    result artifact; when absent the recorded result is serialised instead."""
    study_payload={'id':str(study.id),'name':study.name,'source_filename':study.source_filename,
                   'description':study.description or ''}
    rows=[{'source_key':row['source_key'],'values':row['values']} for row in run.snapshot]
    snapshot_sha=checksum(rows)
    observations_payload={'study_id':str(study.id),'count':len(rows),'checksum':snapshot_sha,'rows':rows}
    run_payload={'id':str(run.id),'study_id':str(study.id),'name':run.name,'status':run.status,
                 'parameters':run.parameters,'checksum':run.checksum,'result':run.result,
                 'error':run.error or '','created_at':run.created_at}
    review_payload=None
    if review is not None:
        review_payload={'run_id':str(review.run_id),'note':review.note,'author':review.author,
                        'created_at':review.created_at}
    result_bytes=artifact_bytes if artifact_bytes is not None else canonical(run.result).encode('utf-8')
    payloads=[('study','items/study.json',canonical(study_payload).encode('utf-8'),study_payload),
              ('observations','items/observations.json',canonical(observations_payload).encode('utf-8'),observations_payload),
              ('run','items/run.json',canonical(run_payload).encode('utf-8'),run_payload),
              ('result','files/result.json',result_bytes,None)]
    if review_payload is not None:
        payloads.append(('review','items/review.json',canonical(review_payload).encode('utf-8'),review_payload))
    items=[{'path':path,'role':role,'sha256':sha256_bytes(payload),'size':len(payload)}
           for role,path,payload,_ in payloads]
    relationships={'study_id':str(study.id),'run_study_id':str(study.id),
                   'observations_study_id':str(study.id),'observations_count':len(rows),
                   'snapshot_sha256':snapshot_sha,'parameters_sha256':digest(run.parameters),
                   'result_sha256':digest(run.result),
                   'study_payload_sha256':study_payload_digest(study_payload),
                   'review_run_id':str(review.run_id) if review is not None else None,
                   'review_sha256':digest(review_payload) if review is not None else None}
    body={'format':FORMAT,'version':VERSION,'created_at':created_at,
          'exporter':{'instance_id':instance_id,'email':exporter_email},
          'source':{'instance_id':instance_id,'study_id':str(study.id),'study_name':study.name,
                    'run_id':str(run.id),'run_name':run.name},
          'relationships':relationships,'items':items}
    body['package_id']=digest(body)
    manifest_bytes=json.dumps(body,indent=2,sort_keys=True,ensure_ascii=False).encode('utf-8')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(*_zip_entry(MANIFEST,manifest_bytes))
        for role,path,payload,_ in sorted(payloads,key=lambda x:x[1]):
            zf.writestr(*_zip_entry(path,payload))
    return buffer.getvalue(),body
