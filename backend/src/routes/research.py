from fastapi import APIRouter,Depends,HTTPException,Request
from fastapi.responses import Response
import hashlib
from sqlalchemy import select
from src.config.database import get_db
from src.config.domain import DOMAIN
from src.middlewares.auth import current_user,writer,admin
from src.schemas.study import StudyInput
from src.schemas.observation import ObservationInput
from src.schemas.run import RunInput
from src.schemas.review import ReviewInput
from src.controllers import research
from src.services import runs,studies,transfers
from src.models.audit import Audit
from src.models.observation import Observation
from src.services.audits import log
from src.utils.serializers import record
router=APIRouter()
MAX_PACKAGE_BYTES=25*1024*1024
@router.get('/domain')
def domain(user=Depends(current_user)): return DOMAIN
@router.get('/overview')
def overview(db=Depends(get_db),user=Depends(current_user)): return research.overview(db)
@router.get('/studies')
def list_studies(db=Depends(get_db),user=Depends(current_user)): return studies.list_studies(db)
@router.post('/studies')
def create_study(data:StudyInput,db=Depends(get_db),user=Depends(writer)): return research.create_study(db,data,user)
@router.get('/studies/{id}/observations')
def rows(id:int,db=Depends(get_db),user=Depends(current_user)): return research.list_rows(db,id)
@router.post('/studies/{id}/observations')
def add(id:int,data:ObservationInput,db=Depends(get_db),user=Depends(writer)): return research.save_row(db,id,data,user)
@router.put('/studies/{id}/observations/{row_id}')
def update(id:int,row_id:int,data:ObservationInput,db=Depends(get_db),user=Depends(writer)): return research.save_row(db,id,data,user,row_id)
@router.delete('/studies/{id}/observations/{row_id}')
def remove(id:int,row_id:int,db=Depends(get_db),user=Depends(writer)):
    studies.require(db,id)
    row=db.get(Observation,row_id)
    if not row or row.study_id!=id: raise HTTPException(404,'记录不存在')
    db.delete(row);log(db,user,'observation.delete',row_id);db.commit();return {'deleted':True}
@router.get('/runs')
def list_runs(db=Depends(get_db),user=Depends(current_user)): return runs.list_runs(db)
@router.post('/runs')
def create_run(data:RunInput,db=Depends(get_db),user=Depends(writer)): return research.create_run(db,data,user)
@router.get('/runs/{id}')
def get_run(id:int,db=Depends(get_db),user=Depends(current_user)): return runs.get(db,id)
@router.post('/runs/{id}/review')
def review(id:int,data:ReviewInput,db=Depends(get_db),user=Depends(admin)): return research.review_run(db,id,data,user)
@router.get('/runs/{id}/export')
def export(id:int,db=Depends(get_db),user=Depends(current_user)):
    return Response(content=runs.export(db,id,user).encode('utf-8'),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="research-{id}.csv"'})
@router.get('/runs/{id}/package')
def package(id:int,db=Depends(get_db),user=Depends(writer)):
    raw,manifest=transfers.export(db,id,user)
    return Response(content=raw,media_type='application/zip',headers={
        'Content-Disposition':f'attachment; filename="research-package-{id}.zip"',
        'X-Package-Id':manifest['package_id'],'X-Package-SHA256':hashlib.sha256(raw).hexdigest()})
@router.post('/transfers')
async def receive_package(request:Request,db=Depends(get_db),user=Depends(writer)):
    raw=await request.body()
    empty={'ok':False,'checks':[],'failures':[],'summary':''}
    if not raw: raise HTTPException(422,{'message':'请上传研究包','report':empty|{'summary':'研究包为空'}})
    if len(raw)>MAX_PACKAGE_BYTES: raise HTTPException(413,{'message':'研究包超过25MB上限','report':empty|{'summary':'研究包过大'}})
    return transfers.receive(db,raw,user)
@router.get('/transfers')
def list_transfers(db=Depends(get_db),user=Depends(current_user)): return transfers.list_transfers(db)
@router.get('/transfers/{id}')
def get_transfer(id:int,db=Depends(get_db),user=Depends(current_user)): return transfers.get(db,id)
@router.post('/transfers/{id}/verify')
def verify_transfer(id:int,db=Depends(get_db),user=Depends(writer)): return transfers.verify(db,id,user)
@router.post('/transfers/{id}/resume')
def resume_transfer(id:int,db=Depends(get_db),user=Depends(writer)): return transfers.resume(db,id,user)
@router.get('/audits')
def audits(db=Depends(get_db),user=Depends(current_user)): return [record(x) for x in db.scalars(select(Audit).order_by(Audit.id.desc()).limit(100))]
