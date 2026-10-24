"""Checkpoint persistence: the latest ProjectState for each run, on disk."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..state import ProjectState, StateError
from ..util import write_json_atomic

RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class CheckpointNotFound(FileNotFoundError):
    pass


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_PATTERN.match(run_id):
        raise ValueError(f"invalid run id {run_id!r}")
    return run_id


class CheckpointStore:
    """Stores ``<runs_dir>/<run_id>/checkpoint.json``.

    Saves are atomic (write temp file, fsync, rename), so an interruption
    leaves either the previous checkpoint or the new one, never a torn file.
    """

    FILENAME = "checkpoint.json"

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = Path(runs_dir)

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / validate_run_id(run_id)

    def path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / self.FILENAME

    def exists(self, run_id: str) -> bool:
        return self.path(run_id).is_file()

    def save(self, state: ProjectState) -> Path:
        path = self.path(state.run_id)
        write_json_atomic(path, state.to_dict())
        return path

    def load(self, run_id: str) -> ProjectState:
        path = self.path(run_id)
        if not path.is_file():
            raise CheckpointNotFound(f"no checkpoint for run {run_id!r} at {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StateError(f"corrupt checkpoint {path}: {exc}") from exc
        return ProjectState.from_dict(data)

    def list_runs(self) -> list[str]:
        if not self.runs_dir.is_dir():
            return []
        return sorted(p.parent.name for p in self.runs_dir.glob(f"*/{self.FILENAME}"))
