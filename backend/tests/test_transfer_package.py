"""End-to-end verification of the migration package.

Every scenario creates a real package through the export endpoint, mutates the
ZIP bytes when needed, and imports it through the receive endpoint.  The
package format is exercised through the API only.
"""
import hashlib
import io
import json
import zipfile

import pytest

from conftest import auth
from src.services.transfer_package import digest


def manifest_of(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        return json.loads(zf.read('manifest.json'))


def rewrite(raw,replacements):
    """Return new ZIP bytes with named entries replaced (None deletes)."""
    buffer=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            payload=replacements[info.filename] if info.filename in replacements else src.read(info)
            if payload is not None: dst.writestr(info.filename,payload)
    return buffer.getvalue()


def repack(raw,payloads):
    """Replace entries and rebuild a self-consistent manifest from the new bytes."""
    manifest=manifest_of(raw)
    for item in manifest['items']:
        if item['path'] in payloads:
            item['sha256']=hashlib.sha256(payloads[item['path']]).hexdigest()
            item['size']=len(payloads[item['path']])
    body=dict(manifest);body.pop('package_id');manifest['package_id']=digest(body)
    payloads['manifest.json']=json.dumps(manifest,ensure_ascii=False).encode()
    return rewrite(raw,payloads)

def export(client,tokens,run_id):
    response=client.get(f'/runs/{run_id}/package',headers=auth(tokens['analyst']))
    assert response.status_code==200,response.text
    return response.content


def receive(client,tokens,raw,role='analyst'):
    return client.post('/transfers',content=raw,headers={**auth(tokens[role]),'Content-Type':'application/zip'})


def copy_run_from_source(source,run_name):
    """Create a second completed run in the source study with different content."""
    from src.config.database import Session
    from src.models.run import Run
    from src.models.study import Study
    from src.services.algorithm import calculate
    from src.services.domain_validation import validate_parameters
    from src.utils.serializers import checksum,now
    with Session() as db:
        study=db.get(Study,source['study_id'])
        rows=[{'source_key':f'MV-{n+1:03}','values':{'moisture':v,'extract':30+n*1.5}}
              for n,v in enumerate((4.0,5.5,5.5,7.3,4.8))]
        params=validate_parameters({'field':'moisture','resamples':400})
        result=calculate(rows,params);result['input_sha256']=checksum(rows)
        other=Run(study_id=study.id,name=run_name,status='completed',parameters=params,
                  snapshot=rows,checksum=checksum(rows),result=result,created_at=now())
        db.add(other);db.commit();run_id=other.id
        from src.config.storage import artifact_root
        (artifact_root()/f'run-{run_id}.json').write_text(json.dumps(result,ensure_ascii=False,allow_nan=False))
        return run_id


def make_local_run(study_name='接收端已有批次',run_name=None):
    run_name=run_name or study_name+'运行'
    from src.config.database import Session
    from src.models.observation import Observation
    from src.models.review import Review
    from src.models.run import Run
    from src.models.study import Study
    from src.services.algorithm import calculate
    from src.services.domain_validation import validate_parameters
    from src.utils.serializers import checksum,now
    with Session() as db:
        study=Study(name=study_name,source_filename='local.csv',description='本地')
        db.add(study);db.flush()
        rows=[{'source_key':f'L-{n}','values':{'moisture':v,'extract':31+n}} for n,v in enumerate((4.9,5.2,5.6,6.1))]
        for row in rows: db.add(Observation(study_id=study.id,source_key=row['source_key'],values=row['values']))
        params=validate_parameters({'field':'moisture','resamples':300})
        result=calculate(rows,params);result['input_sha256']=checksum(rows)
        run=Run(study_id=study.id,name=run_name,status='completed',parameters=params,
                snapshot=rows,checksum=checksum(rows),result=result,created_at=now())
        db.add(run);db.flush()
        db.add(Review(run_id=run.id,note='本地复核意见（接收端）。',author='admin@test.local',created_at=now()))
        db.commit()
        from src.config.storage import artifact_root
        (artifact_root()/f'run-{run.id}.json').write_text(json.dumps(result,ensure_ascii=False,allow_nan=False))
        return run.id,study.id


def test_package_round_trip_creates_hidden_then_ready(client,tokens,seeded_source):
    raw=export(client,tokens,seeded_source['run_id'])
    manifest=manifest_of(raw)
    assert manifest['format']=='tea-quantiles-research-package'
    assert {x['role'] for x in manifest['items']}=={'study','observations','run','result','review'}
    response=receive(client,tokens,raw)
    assert response.status_code==200,response.text
    body=response.json()
    assert body['outcome']=='imported' and body['status']=='ready'
    assert body['local']['study_id']!=seeded_source['study_id']
    assert body['source_review']['note'].startswith('来源端已复核')
    assert body['review_is_local'] is False
    runs={r['id'] for r in client.get('/runs',headers=auth(tokens['analyst'])).json()}
    assert body['local']['run_id'] in runs
    studies={s['id'] for s in client.get('/studies',headers=auth(tokens['analyst'])).json()}
    assert body['local']['study_id'] in studies
    local_run=client.get(f"/runs/{body['local']['run_id']}",headers=auth(tokens['analyst'])).json()
    assert local_run['review'] is None and local_run['imported'] is True
    approve=client.post(f"/runs/{body['local']['run_id']}/review",
                        json={'note':'接收端本地复核：已独立核验内容与复算。'},headers=auth(tokens['admin']))
    assert approve.status_code==200,approve.text
    transfer=client.get(f"/transfers/{body['id']}",headers=auth(tokens['analyst'])).json()
    assert transfer['archive_verified'] is True and transfer['validation']['ok'] is True
    # The verified upload appears in the audit trail.
    actions={x['action'] for x in client.get('/audits',headers=auth(tokens['analyst'])).json()}
    assert {'run.package','transfer.receive','transfer.ready'} <= actions


def test_duplicate_and_concurrent_imports_do_not_duplicate(client,tokens,seeded_source):
    raw=export(client,tokens,seeded_source['run_id'])
    first=receive(client,tokens,raw);assert first.status_code==200,first.text
    second=receive(client,tokens,raw);assert second.status_code==200,second.text
    assert second.json()['outcome']=='duplicate'
    assert second.json()['local']==first.json()['local']
    assert second.json()['id']==first.json()['id']
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:receive(client,tokens,raw),range(4)))
    assert [r.status_code for r in results]==[200]*4
    assert len({r.json()['id'] for r in results})==1
    runs=client.get('/runs',headers=auth(tokens['analyst'])).json()
    imported=[r for r in runs if r['name']==seeded_source['run_name'] and r['study_id']!=seeded_source['study_id'] and r['imported']]
    assert len(imported)==1
    studies=client.get('/studies',headers=auth(tokens['analyst'])).json()
    assert len([s for s in studies if s['name']==seeded_source['study_name']])==1
    assert len([s for s in studies if s['name'].startswith(seeded_source['study_name'])])==2


def forge(raw,payloads,source=None):
    """Rewrite entries and rebuild a fully self-consistent manifest, as an
    attacker with write access to the ZIP could.  Only the identity used for
    duplicate/conflict detection is deliberately mismatched with an accepted
    package."""
    manifest=manifest_of(raw)
    if source: manifest['source'].update(source)
    for item in manifest['items']:
        if item['path'] in payloads:
            item['sha256']=hashlib.sha256(payloads[item['path']]).hexdigest()
            item['size']=len(payloads[item['path']])
    body=dict(manifest);body.pop('package_id');manifest['package_id']=digest(body)
    return rewrite(raw,{**payloads,'manifest.json':json.dumps(manifest,ensure_ascii=False).encode()})


def test_same_source_number_different_content_is_rejected(client,tokens,seeded_source):
    raw=export(client,tokens,seeded_source['run_id'])
    accepted=receive(client,tokens,raw);assert accepted.status_code==200,accepted.text
    other_run=copy_run_from_source(seeded_source,seeded_source['run_name'])
    conflicting=export(client,tokens,other_run)
    # A valid, self-consistent package whose source run number collides with
    # the accepted one but whose frozen content differs.
    with zipfile.ZipFile(io.BytesIO(conflicting)) as zf:
        run=json.loads(zf.read('items/run.json'))
        review=json.loads(zf.read('items/review.json')) if 'items/review.json' in zf.namelist() else None
    run['id']=str(seeded_source['run_id'])
    payloads={'items/run.json':json.dumps(run,ensure_ascii=False).encode()}
    if review is not None:
        review['run_id']=str(seeded_source['run_id'])
        payloads['items/review.json']=json.dumps(review,ensure_ascii=False).encode()
    forged=forge(conflicting,payloads,source={'run_id':str(seeded_source['run_id'])})
    response=receive(client,tokens,forged)
    assert response.status_code==409,response.text
    assert '拒绝混合' in response.json()['detail']['message']
    # Only the first accepted package is present for this source run.
    runs=client.get('/runs',headers=auth(tokens['analyst'])).json()
    imported=[r for r in runs if r['name']==seeded_source['run_name'] and r['study_id']!=seeded_source['study_id']]
    assert [r['id'] for r in imported]==[accepted.json()['local']['run_id']]


def test_number_collision_keeps_local_numbering(client,tokens,seeded_source):
    raw=export(client,tokens,seeded_source['run_id'])
    # Receiving seat already has its own study/run under its own numbering.
    local_run,local_study=make_local_run(study_name='接收端已有批次',run_name='接收端本地运行')
    response=receive(client,tokens,raw)
    assert response.status_code==200,response.text
    body=response.json()
    assert body['local']['study_id']!=local_study
    assert body['local']['run_id']!=local_run
    studies={s['id']:s for s in client.get('/studies',headers=auth(tokens['analyst'])).json()}
    assert studies[local_study]['name']=='接收端已有批次'
    # The incoming name already belongs to the source study, so the import
    # gets a distinct local name instead of taking over an existing number.
    assert studies[body['local']['study_id']]['name']!=studies[local_study]['name']
    assert studies[body['local']['study_id']]['name']!=seeded_source['study_name']
    # Local numbering is untouched by the import.
    assert client.get(f'/runs/{local_run}',headers=auth(tokens['analyst'])).json()['name']=='接收端本地运行'


def test_missing_item_rejects_whole_package(client,tokens,seeded_source):
    raw=export(client,tokens,seeded_source['run_id'])
    broken=rewrite(raw,{'items/observations.json':None})
    response=receive(client,tokens,broken)
    assert response.status_code==422,response.text
    detail=response.json()['detail']
    assert detail['report']['ok'] is False
    assert any('observations' in (f['item']+f['detail']) for f in detail['report']['failures'])
    visible=[r for r in client.get('/runs',headers=auth(tokens['analyst'])).json() if r['study_id']!=seeded_source['study_id']]
    assert all(r['name']!=seeded_source['run_name'] for r in visible)


def test_tampered_attachment_rejects_whole_package(client,tokens,seeded_source):
    raw=export(client,tokens,seeded_source['run_id'])
    with zipfile.ZipFile(io.BytesIO(raw)) as zf: artifact=json.loads(zf.read('files/result.json'))
    artifact['metrics']['中位数']=999.0
    tampered=rewrite(raw,{'files/result.json':json.dumps(artifact,ensure_ascii=False).encode()})
    response=receive(client,tokens,tampered)
    assert response.status_code==422,response.text
    failures=response.json()['detail']['report']['failures']
    assert any(f['item']=='files/result.json' for f in failures)


def test_value_change_with_rebuilt_manifest_is_caught_by_recompute(client,tokens,seeded_source):
    """An attacker with full control of the ZIP rebuilds every hash so that all
    consistency checks pass; only recomputation from the frozen observations
    reveals that the recorded result no longer follows from them."""
    raw=export(client,tokens,seeded_source['run_id'])
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        observations=json.loads(zf.read('items/observations.json'))
        run=json.loads(zf.read('items/run.json'))
        artifact=json.loads(zf.read('files/result.json'))
    from src.utils.serializers import checksum
    rows=observations['rows'];rows[0]['values']['moisture']=9.9
    observations['checksum']=checksum(rows)
    run['checksum']=observations['checksum']
    # Keep the recorded result internally consistent with the forged snapshot.
    run['result']['input_sha256']=observations['checksum']
    manifest=manifest_of(raw)
    manifest['relationships']['snapshot_sha256']=observations['checksum']
    manifest['relationships']['observations_count']=len(rows)
    manifest['relationships']['study_payload_sha256']=digest(
        json.loads(zipfile.ZipFile(io.BytesIO(raw)).read('items/study.json')))
    manifest['relationships']['result_sha256']=digest(run['result'])
    artifact=run['result']
    payloads={'items/observations.json':json.dumps(observations,ensure_ascii=False).encode(),
              'items/run.json':json.dumps(run,ensure_ascii=False).encode(),
              'files/result.json':json.dumps(artifact,ensure_ascii=False).encode()}
    response=receive(client,tokens,repack_with(raw,payloads,manifest))
    assert response.status_code==422,response.text
    checks=[f['check'] for f in response.json()['detail']['report']['failures']]
    assert 'result-reproducible' in checks,checks


def repack_with(raw,payloads,manifest):
    buffer=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if info.filename=='manifest.json': continue
            dst.writestr(info.filename,payloads.get(info.filename,src.read(info)))
        for item in manifest['items']:
            if item['path'] in payloads:
                item['sha256']=hashlib.sha256(payloads[item['path']]).hexdigest()
                item['size']=len(payloads[item['path']])
        body=dict(manifest);body.pop('package_id');manifest['package_id']=digest(body)
        dst.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False).encode())
    return buffer.getvalue()


def test_archive_write_failure_hides_then_resume_recovers(client,tokens,seeded_source,monkeypatch):
    from pathlib import Path
    raw=export(client,tokens,seeded_source['run_id'])
    from src.services import transfers as transfer_service
    original=transfer_service._atomic_write
    def failing(path,payload):
        if str(path).endswith('.receipt.json'): raise OSError('disk full (simulated)')
        return original(path,payload)
    monkeypatch.setattr(transfer_service,'_atomic_write',failing)
    response=receive(client,tokens,raw)
    assert response.status_code==503,response.text
    transfer_id=response.json()['detail']['transfer_id']
    assert client.get(f'/transfers/{transfer_id}',headers=auth(tokens['analyst'])).json()['status']=='incomplete'
    visible=[r for r in client.get('/runs',headers=auth(tokens['analyst'])).json() if r['study_id']!=seeded_source['study_id']]
    assert all(r['name']!=seeded_source['run_name'] for r in visible)
    monkeypatch.setattr(transfer_service,'_atomic_write',original)
    resumed=client.post(f'/transfers/{transfer_id}/resume',headers=auth(tokens['analyst']))
    assert resumed.status_code==200,resumed.text
    assert resumed.json()['status']=='ready'
    assert resumed.json()['outcome']=='recovered'
    assert client.get(f"/runs/{seeded_source['run_id']}",headers=auth(tokens['analyst'])).status_code==200
    assert any(r['name']==seeded_source['run_name'] for r in client.get('/runs',headers=auth(tokens['analyst'])).json())


def test_verify_detects_tampered_archive_and_rehides(client,tokens,seeded_source):
    raw=export(client,tokens,seeded_source['run_id'])
    body=receive(client,tokens,raw).json()
    transfer=client.get(f"/transfers/{body['id']}",headers=auth(tokens['analyst'])).json()
    with open(transfer['archive_path'],'r+b') as handle:
        handle.seek(0);handle.write(b'PK\x05\x06'+b'\x00'*18)
    verified=client.post(f"/transfers/{body['id']}/verify",headers=auth(tokens['analyst']))
    assert verified.status_code==200,verified.text
    assert verified.json()['verification']['ok'] is False
    assert verified.json()['status']=='incomplete'
    assert all(r['id']!=body['local']['run_id'] for r in client.get('/runs',headers=auth(tokens['analyst'])).json())
    assert client.get(f"/runs/{body['local']['run_id']}",headers=auth(tokens['analyst'])).status_code==404
    # Nothing was deleted; the hidden row is still there for recovery.
    from src.config.database import Session
    from src.models.run import Run
    with Session() as db: assert db.get(Run,body['local']['run_id']) is not None


def test_missing_receipt_makes_research_incomplete(client,tokens,seeded_source):
    from pathlib import Path
    raw=export(client,tokens,seeded_source['run_id'])
    body=receive(client,tokens,raw).json()
    transfer=client.get(f"/transfers/{body['id']}",headers=auth(tokens['analyst'])).json()
    receipt=next(Path(transfer['archive_path']).parent.glob('*.receipt.json'))
    receipt.unlink()
    verified=client.post(f"/transfers/{body['id']}/verify",headers=auth(tokens['analyst']))
    assert verified.status_code==200,verified.text
    assert verified.json()['verification']['ok'] is False
    assert any(f['item']=='receipt' for f in verified.json()['verification']['failures'])
    assert verified.json()['status']=='incomplete'
    # Restoring the receipt through a retry makes it usable again.
    from src.services import transfers as transfer_service
    resumed=client.post(f"/transfers/{body['id']}/resume",headers=auth(tokens['analyst']))
    assert resumed.status_code==200,resumed.text
    assert resumed.json()['status']=='ready'
    assert any(r['id']==body['local']['run_id'] for r in client.get('/runs',headers=auth(tokens['analyst'])).json())


def test_reexported_package_is_duplicate_not_a_second_research(client,tokens,seeded_source):
    """Re-exporting the same run yields a different package id (new timestamp)
    but identical content; the receiving seat must reuse the existing research."""
    first=receive(client,tokens,export(client,tokens,seeded_source['run_id'])).json()
    again=receive(client,tokens,export(client,tokens,seeded_source['run_id'])).json()
    assert again['outcome']=='duplicate'
    assert again['id']==first['id']
    assert again['package_id']==first['package_id']
    runs=[r for r in client.get('/runs',headers=auth(tokens['analyst'])).json()
          if r['study_id']==first['local']['study_id']]
    assert len(runs)==1


def test_viewer_cannot_import_or_package(client,tokens,seeded_source):
    raw=export(client,tokens,seeded_source['run_id'])
    assert receive(client,tokens,raw,role='viewer').status_code==403


def test_verify_after_local_edits_keeps_research_visible(client,tokens,seeded_source):
    """Adding new local observations to an imported study is normal work and
    must not be mistaken for tampering; frozen rows are what gets checked."""
    raw=export(client,tokens,seeded_source['run_id'])
    body=receive(client,tokens,raw).json()
    study_id=body['local']['study_id']
    added=client.post(f'/studies/{study_id}/observations',
                      json={'source_key':'NEW-LOCAL-1','values':{'moisture':5.0,'extract':31.0}},
                      headers=auth(tokens['analyst']))
    assert added.status_code==200,added.text
    verified=client.post(f"/transfers/{body['id']}/verify",headers=auth(tokens['analyst']))
    assert verified.status_code==200,verified.text
    assert verified.json()['verification']['ok'] is True,verified.text
    assert verified.json()['status']=='ready'
    assert client.get(f"/runs/{body['local']['run_id']}",headers=auth(tokens['analyst'])).status_code==200


def test_unreadable_zip_entry_rejects_without_500(client,tokens,seeded_source):
    """Corrupt compressed data inside an otherwise well-formed ZIP must be a
    verification failure, not an internal error."""
    raw=export(client,tokens,seeded_source['run_id'])
    buffer=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src,zipfile.ZipFile(buffer,'w',zipfile.ZIP_STORED) as dst:
        for info in src.infolist():
            data=src.read(info)
            if info.filename=='items/observations.json':
                data=data[:len(data)//2]+b'\x00'*(len(data)-len(data)//2)
            dst.writestr(info.filename,data)
    response=receive(client,tokens,buffer.getvalue())
    assert response.status_code==422,response.text
    assert response.json()['detail']['report']['ok'] is False


def test_not_a_zip_is_rejected(client,tokens,seeded_source):
    response=receive(client,tokens,b'this is not a package')
    assert response.status_code==422,response.text
    assert 'ZIP' in response.json()['detail']['message']
