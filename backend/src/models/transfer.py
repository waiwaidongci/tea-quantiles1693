from sqlalchemy import Column,Integer,String,Text,JSON,ForeignKey,UniqueConstraint
from src.config.database import Base

class ResearchTransfer(Base):
    """One received research package and the local rows it created.

    status:
      pending    - database staged, archive not yet published; hidden
      ready      - database and archive complete; visible and usable
      incomplete - archive missing/failed; existing rows kept but hidden
    """
    __tablename__='transfers'
    __table_args__=(
        UniqueConstraint('package_id',name='uq_transfer_package'),
        UniqueConstraint('source_instance_id','source_study_id','source_run_id',name='uq_transfer_source'),
    )
    id=Column(Integer,primary_key=True)
    package_id=Column(String(64),nullable=False)
    package_sha256=Column(String(64),nullable=False)
    source_instance_id=Column(String(120),nullable=False)
    source_study_id=Column(String(80),nullable=False)
    source_run_id=Column(String(80),nullable=False)
    source_study_name=Column(String(120),nullable=False)
    source_run_name=Column(String(120),nullable=False)
    status=Column(String(20),default='pending',nullable=False)
    content_sha256=Column(String(64),nullable=False)
    local_study_id=Column(Integer,ForeignKey('studies.id'),nullable=True)
    local_run_id=Column(Integer,ForeignKey('runs.id'),nullable=True)
    manifest=Column(JSON,nullable=False)
    validation=Column(JSON,nullable=False)
    packaged_review=Column(JSON,nullable=True)
    staging_path=Column(String(255),default='')
    archive_path=Column(String(255),default='')
    archive_sha256=Column(String(64),default='')
    error=Column(Text,default='')
    received_by=Column(String(120),default='')
    received_at=Column(String(40),nullable=False)
    verified_at=Column(String(40),default='')
    updated_at=Column(String(40),default='')

class StudySource(Base):
    """Maps a source study number to one local study.

    A source study number may only ever map to a single study payload.
    A different payload with the same number is rejected to prevent mixing.
    """
    __tablename__='study_sources'
    __table_args__=(UniqueConstraint('source_instance_id','source_study_id',name='uq_study_source'),)
    id=Column(Integer,primary_key=True)
    source_instance_id=Column(String(120),nullable=False)
    source_study_id=Column(String(80),nullable=False)
    study_payload_sha256=Column(String(64),nullable=False)
    local_study_id=Column(Integer,ForeignKey('studies.id'),nullable=False)
    created_at=Column(String(40),nullable=False)

class WorkspaceIdentity(Base):
    __tablename__='workspace_identity'
    id=Column(Integer,primary_key=True)
    instance_id=Column(String(120),nullable=False)
    created_at=Column(String(40),nullable=False)
