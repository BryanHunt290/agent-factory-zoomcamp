"""Sandbox image configuration: the immutable image record written by a human-run build.

``sandbox_image/image.lock.json`` records exactly which image DockerBackend
may use (by image id) and what it was built from (digest-pinned base,
Dockerfile and af-entry hashes). Agent runs only ever read it.

If the record is missing, malformed, or stale (the Dockerfile or af-entry
changed since the build), the resulting DockerConfig has no image id and a
reason, so DockerBackend reports itself unavailable and runs BLOCK.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from ..util import write_json_atomic
from .docker import DEFAULT_IMAGE_REF, IMAGE_ID_RE, DockerConfig

SANDBOX_IMAGE_DIR = Path(__file__).resolve().parents[2] / "sandbox_image"
LOCK_PATH = SANDBOX_IMAGE_DIR / "image.lock.json"
LOCK_FORMAT = 1


@dataclass(frozen=True)
class ImageRecord:
    image_id: str
    image_ref: str
    base_image: str  # digest-pinned reference, e.g. docker.io/library/python@sha256:...
    base_image_id: str
    base_python_version: str
    dockerfile_sha256: str
    af_entry_sha256: str
    built_at: str
    docker_server_version: str
    protocol: int = 1
    format: int = LOCK_FORMAT


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes(image_dir: Path = SANDBOX_IMAGE_DIR) -> dict[str, str]:
    return {
        "dockerfile_sha256": file_sha256(image_dir / "Dockerfile"),
        "af_entry_sha256": file_sha256(image_dir / "af-entry"),
    }


def load_image_record(path: Path = LOCK_PATH) -> ImageRecord | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in fields(ImageRecord)}
    if not isinstance(data, dict) or set(data) - known:
        raise ValueError(f"{path}: unexpected fields")
    record = ImageRecord(**data)
    if record.format != LOCK_FORMAT or not IMAGE_ID_RE.match(record.image_id) or not IMAGE_ID_RE.match(record.base_image_id):
        raise ValueError(f"{path}: invalid image record")
    return record


def save_image_record(record: ImageRecord, path: Path = LOCK_PATH) -> None:
    write_json_atomic(path, asdict(record))


def docker_config_from_lock(path: Path = LOCK_PATH, image_dir: Path = SANDBOX_IMAGE_DIR) -> DockerConfig:
    """DockerConfig for the recorded image, or one that fails closed with a reason."""
    try:
        record = load_image_record(path)
    except (ValueError, TypeError, OSError) as exc:
        return DockerConfig(image_id=None, unavailable_reason=f"sandbox image record is invalid: {exc}")
    if record is None:
        return DockerConfig(
            image_id=None,
            unavailable_reason="no sandbox image has been built; run `python -m agent_factory sandbox build-image`",
        )
    current = source_hashes(image_dir)
    if current != {"dockerfile_sha256": record.dockerfile_sha256, "af_entry_sha256": record.af_entry_sha256}:
        return DockerConfig(
            image_id=None,
            unavailable_reason="sandbox image is stale (Dockerfile or af-entry changed since it was built); rebuild it",
        )
    return DockerConfig(image_id=record.image_id, image_ref=record.image_ref or DEFAULT_IMAGE_REF)
