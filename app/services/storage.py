"""File storage for generated certificates.

The rest of the code only deals in opaque *keys* (e.g. "<job_id>/<certificate_id>.pdf"),
never absolute paths, so this local implementation can be swapped for S3/GCS by adding a
class with the same interface.
"""

import os
import uuid
from functools import lru_cache
from pathlib import Path

from app.config import get_settings


class LocalFileStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"Storage key escapes the storage root: {key!r}")
        return path

    def save(self, key: str, data: bytes) -> str:
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file and atomically rename, so readers never see a half-written PDF.
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return key

    def exists(self, key: str) -> bool:
        return self.path(key).is_file()


@lru_cache
def get_storage() -> LocalFileStorage:
    return LocalFileStorage(get_settings().storage_dir)
