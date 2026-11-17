Schema v2 adds portable research packages.

v1 tables (studies, observations, runs, reviews, users, audits) are still created by `Base.metadata.create_all` at startup. `create_all` never alters an existing table, so a database created by v1 needs the additive statements below (or a fresh volume) before starting v2.

```sql
-- v1 -> v2: hidden-until-verified flag on imported research
ALTER TABLE studies ADD COLUMN visible BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE runs    ADD COLUMN visible BOOLEAN NOT NULL DEFAULT TRUE;

-- new tables (also created automatically by create_all on an empty database)
CREATE TABLE transfers (
    id SERIAL PRIMARY KEY,
    package_id VARCHAR(64) NOT NULL UNIQUE,
    package_sha256 VARCHAR(64) NOT NULL,
    content_sha256 VARCHAR(64) NOT NULL,
    source_instance_id VARCHAR(120) NOT NULL,
    source_study_id VARCHAR(80) NOT NULL,
    source_run_id VARCHAR(80) NOT NULL,
    source_study_name VARCHAR(120) NOT NULL,
    source_run_name VARCHAR(120) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    local_study_id INTEGER REFERENCES studies(id),
    local_run_id INTEGER REFERENCES runs(id),
    manifest JSON NOT NULL,
    validation JSON NOT NULL,
    packaged_review JSON,
    staging_path VARCHAR(255) DEFAULT '',
    archive_path VARCHAR(255) DEFAULT '',
    archive_sha256 VARCHAR(64) DEFAULT '',
    error TEXT DEFAULT '',
    received_by VARCHAR(120) DEFAULT '',
    received_at VARCHAR(40) NOT NULL,
    verified_at VARCHAR(40) DEFAULT '',
    updated_at VARCHAR(40) DEFAULT '',
    CONSTRAINT uq_transfer_package UNIQUE (package_id),
    CONSTRAINT uq_transfer_source UNIQUE (source_instance_id, source_study_id, source_run_id)
);
CREATE TABLE workspace_identity (
    id SERIAL PRIMARY KEY,
    instance_id VARCHAR(120) NOT NULL,
    created_at VARCHAR(40) NOT NULL
);
```

No destructive migration is run. Back up the named `db-data` volume before altering. Existing rows keep `visible = TRUE` so local research stays visible after the upgrade.

Package archives and receipts live on the shared `run-artifacts` volume under `ARTIFACT_ROOT`:

```
artifacts/
  run-<id>.json                       # result artifact written by the worker
  transfers/<sha256-prefix>/<package>.zip         # accepted source package
  transfers/<sha256-prefix>/<package>.receipt.json # checks, source review, verification
  transfers/.staging/<package>.zip    # uploaded bytes awaiting completion/recovery
```
