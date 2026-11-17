from sqlalchemy import Column,Integer,String,Text,Boolean
from src.config.database import Base
class TeaLot(Base):
    __tablename__='studies'
    id=Column(Integer,primary_key=True)
    name=Column(String(120),unique=True,nullable=False)
    source_filename=Column(String(120),nullable=False)
    description=Column(Text,default='')
    # Imported research is only visible once its package archive is complete.
    visible=Column(Boolean,default=True,nullable=False)
Study=TeaLot
