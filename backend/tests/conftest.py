import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BACKEND))


def _prepare_env(tmp_path):
    os.environ['DATABASE_URL']=f"sqlite:///{tmp_path/'test.db'}"
    os.environ.setdefault('JWT_SECRET','test-secret-for-package-transfer')
    os.environ['REDIS_URL']='redis://localhost:6399/0'
    os.environ['ARTIFACT_ROOT']=str(tmp_path/'artifacts')
    os.environ['WORKSPACE_INSTANCE_ID']='ws-test-local'


@pytest.fixture(scope='session')
def workdir():
    with tempfile.TemporaryDirectory(prefix='transfer-tests-') as path:
        yield Path(path)


@pytest.fixture(scope='session',autouse=True)
def app_module(workdir):
    _prepare_env(workdir)
    from src.config.database import Base,engine
    from src.models import audit,observation,review,run,study,transfer,user
    Base.metadata.create_all(engine)
    import fakeredis
    from src.middlewares import rate_limit
    rate_limit.redis=fakeredis.FakeRedis()
    from src.main import app
    return app


@pytest.fixture()
def db(workdir):
    from src.config.database import Session
    with Session() as session:
        yield session


@pytest.fixture()
def client(app_module):
    from fastapi.testclient import TestClient
    return TestClient(app_module)


@pytest.fixture()
def users(db):
    from sqlalchemy import select
    from src.models.user import User
    from src.utils.security import hash_password
    created={}
    for role in ('admin','analyst','viewer'):
        email=f'{role}@test.local'
        user=db.scalar(select(User).where(User.email==email))
        if not user:
            user=User(email=email,role=role,password_hash=hash_password('test-pass'))
            db.add(user);db.commit()
        created[role]=user
    return created


@pytest.fixture()
def tokens(client,users):
    out={}
    for role in ('admin','analyst','viewer'):
        r=client.post('/auth/login',json={'email':f'{role}@test.local','password':'test-pass'})
        assert r.status_code==200,r.text
        out[role]=r.json()['token']
    return out


def auth(token):
    return {'Authorization':'Bearer '+token}


@pytest.fixture()
def seeded_source(client,tokens):
    """A completed run with three observations and an approval in this workspace.

    Names carry a per-test token so that tests never depend on each other's
    imported rows.
    """
    import uuid
    from src.config.database import Session
    from src.models.observation import Observation
    from src.models.review import Review
    from src.models.run import Run
    from src.models.study import Study
    from src.services.algorithm import calculate
    from src.services.domain_validation import validate_parameters
    from src.utils.serializers import checksum,now
    token=uuid.uuid4().hex[:8]
    study_name=f'青禾·迁移源批次-{token}'
    run_name=f'迁移来源运行-{token}'
    with Session() as db:
        study=Study(name=study_name,source_filename=f'migration-source-{token}.csv',description='迁移测试来源')
        db.add(study);db.flush()
        rows=[]
        for n,value in enumerate((5.1,5.4,5.9,6.2,4.8)):
            row=Observation(study_id=study.id,source_key=f'MV-{n+1:03}',values={'moisture':value,'extract':30+n*1.5})
            db.add(row);rows.append({'source_key':row.source_key,'values':row.values})
        params=validate_parameters({'field':'moisture','resamples':400})
        result=calculate(rows,params);result['input_sha256']=checksum(rows)
        run=Run(study_id=study.id,name=run_name,status='completed',parameters=params,
                snapshot=rows,checksum=checksum(rows),result=result,created_at=now())
        db.add(run);db.flush()
        review=Review(run_id=run.id,note='来源端已复核：仅用于迁移演示，不可当作接收端复核。',author='admin@test.local',created_at=now())
        db.add(review);db.commit()
        from src.config.storage import artifact_root
        import json
        artifact_root().mkdir(parents=True,exist_ok=True)
        (artifact_root()/f'run-{run.id}.json').write_text(json.dumps(result,ensure_ascii=False,allow_nan=False))
        return {'study_id':study.id,'run_id':run.id,'study_name':study_name,'run_name':run_name}
