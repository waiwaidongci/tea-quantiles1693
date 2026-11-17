"""Additive schema upgrades for databases created before a column existed.

The application intentionally has no destructive migration runner.  `create_all`
never alters existing tables, so v1 databases need the `visible` columns that
the package-import workflow relies on.  This helper only ever adds missing
columns with a safe default and never drops or rewrites data.
"""
import logging

from sqlalchemy import inspect, text

from src.config.database import Base, engine

ADDITIVE={
    'studies':{'visible':'BOOLEAN NOT NULL DEFAULT TRUE'},
    'runs':{'visible':'BOOLEAN NOT NULL DEFAULT TRUE'},
}


def ensure_schema():
    Base.metadata.create_all(engine)
    inspector=inspect(engine)
    with engine.begin() as conn:
        for table,columns in ADDITIVE.items():
            existing={c['name'] for c in inspector.get_columns(table)}
            for name,definition in columns.items():
                if name in existing: continue
                logging.warning('adding missing column %s.%s',table,name)
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {definition}'))
