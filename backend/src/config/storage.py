import os
from pathlib import Path

DEFAULT_ARTIFACT_ROOT='/app/artifacts'

def artifact_root():
    """Shared artifact volume. Resolved per call so tests and recovery can
    point at another directory without reloading the application."""
    return Path(os.environ.get('ARTIFACT_ROOT',DEFAULT_ARTIFACT_ROOT))
