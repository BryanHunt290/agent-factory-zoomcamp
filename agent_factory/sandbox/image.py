"""Human-initiated sandbox image preparation.

    *** Only the CLI (`python -m agent_factory sandbox ...`) uses this module. ***

Nothing on the agent-run path (executor, agents, tools, SandboxManager,
DockerBackend) imports it; a test enforces that. DockerBackend's own
subcommand allowlist excludes ``build``/``pull`` as a second barrier.

Policy:

* the base image is named by its full ``sha256:`` id and must already be
  present locally, with a RepoDigest proving which repository it came from;
  nothing is ever pulled. If the base is absent, the error says exactly what
  a human would have to pull;
* the build runs with ``--pull=false``, ``--network none`` and
  ``--no-cache``, from a context containing only the Dockerfile and af-entry;
* the resulting immutable image id is verified and recorded in
  ``sandbox_image/image.lock.json``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..util import timestamp, utc_now
from .config import LOCK_PATH, SANDBOX_IMAGE_DIR, ImageRecord, save_image_record, source_hashes
from .docker import (
    DEFAULT_IMAGE_REF,
    IMAGE_ID_RE,
    SANDBOX_UID,
    DockerRunner,
    SubprocessDockerRunner,
    endpoint_from_context,
    endpoint_problem,
)
from .process import ProcessOutcome, decode

BUILD_SUBCOMMANDS = frozenset({("version",), ("context", "inspect"), ("image", "inspect"), ("build",)})
MiB = 1024 * 1024
#: Build-log phrases that would indicate registry access. None should appear.
REGISTRY_ACCESS_MARKERS = ("pulling", "downloading", "pull access", "unable to find image")


class ImageBuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class BaseImageInfo:
    image_id: str
    repo_tags: tuple[str, ...]
    repo_digests: tuple[str, ...]
    digest_ref: str  # canonical digest-pinned reference used for FROM
    os: str
    architecture: str
    created: str
    python_version: str
    env_names: tuple[str, ...]
    user: str


@dataclass(frozen=True)
class BuildResult:
    record: ImageRecord
    log: str


class ImageBuilder:
    def __init__(self, runner: DockerRunner | None = None, *, image_dir: Path = SANDBOX_IMAGE_DIR) -> None:
        self._runner = runner or SubprocessDockerRunner()
        self.image_dir = image_dir
        self.endpoint: str | None = None
        self.server_version = "unknown"
        self.server_arch = ""

    def _docker(self, args: Sequence[str], *, pinned: bool = True, timeout_s: float = 60.0) -> ProcessOutcome:
        args = list(args)
        key = tuple(args[:2]) if args and args[0] in ("context", "image") else tuple(args[:1])
        if key not in BUILD_SUBCOMMANDS:
            raise ImageBuildError(f"refusing docker subcommand {' '.join(key)!r}")
        if pinned and self.endpoint is None:
            raise ImageBuildError("not connected to a local Docker engine")
        return self._runner(
            args,
            host=self.endpoint if pinned else None,
            stdin=None,
            timeout_s=timeout_s,
            output_limit=4 * MiB,
            on_kill=None,
        )

    def connect(self) -> str:
        env_host = os.environ.get("DOCKER_HOST")
        if env_host and endpoint_problem(env_host):
            raise ImageBuildError(f"DOCKER_HOST: {endpoint_problem(env_host)}")
        version = self._docker(["version", "--format", "{{json .}}"], pinned=False)
        try:
            server = json.loads(decode(version.stdout)).get("Server") if version.exit_code == 0 else None
        except (ValueError, AttributeError):
            server = None
        if not server:
            raise ImageBuildError(f"docker engine not reachable: {version.error or decode(version.stderr).strip()[:200]}")
        endpoint = env_host
        if not endpoint:
            ctx = self._docker(["context", "inspect"], pinned=False)
            endpoint = endpoint_from_context(decode(ctx.stdout)) if ctx.exit_code == 0 else None
        problem = endpoint_problem(endpoint)
        if problem:
            raise ImageBuildError(problem)
        self.endpoint = endpoint
        self.server_version = str(server.get("Version", "unknown"))
        self.server_arch = str(server.get("Arch", ""))
        return endpoint

    def _inspect(self, ref: str) -> dict | None:
        out = self._docker(["image", "inspect", "--format", "{{json .}}", ref])
        if out.exit_code != 0:
            return None
        try:
            data = json.loads(decode(out.stdout))
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def inspect_base(self, base_id: str) -> BaseImageInfo:
        """Read-only identification of a local base image. Never pulls."""
        if not IMAGE_ID_RE.match(base_id or ""):
            raise ImageBuildError("the base image must be given as a full sha256:<64 hex> id (tags are not accepted)")
        if self.endpoint is None:
            self.connect()
        data = self._inspect(base_id)
        if data is None:
            raise ImageBuildError(
                f"base image {base_id} is not present locally. Agent Factory never pulls images. "
                f"A human would have to authorize and run: docker pull docker.io/library/python@{base_id} "
                "(after independently verifying that digest)"
            )
        if data.get("Id") != base_id:
            raise ImageBuildError(f"image id mismatch: asked for {base_id}, engine reports {data.get('Id')}")
        digests = tuple(data.get("RepoDigests") or ())
        matching = [d for d in digests if d.endswith("@" + base_id)]
        if len(matching) != 1:
            raise ImageBuildError(
                f"cannot establish provenance: expected exactly one RepoDigest ending in @{base_id}, found {list(digests)}"
            )
        repo = matching[0].split("@", 1)[0]
        if "/" not in repo:
            repo = f"docker.io/library/{repo}"
        config = data.get("Config") or {}
        env = dict(e.split("=", 1) for e in (config.get("Env") or []) if "=" in e)
        info = BaseImageInfo(
            image_id=base_id,
            repo_tags=tuple(data.get("RepoTags") or ()),
            repo_digests=digests,
            digest_ref=f"{repo}@{base_id}",
            os=str(data.get("Os", "")),
            architecture=str(data.get("Architecture", "")),
            created=str(data.get("Created", "")),
            python_version=env.get("PYTHON_VERSION", ""),
            env_names=tuple(sorted(env)),
            user=str(config.get("User") or ""),
        )
        if info.os != "linux":
            raise ImageBuildError(f"base image OS is {info.os!r}, not linux")
        if self.server_arch and info.architecture != self.server_arch:
            raise ImageBuildError(f"base image architecture {info.architecture} != engine {self.server_arch}")
        if not info.python_version.startswith("3.12."):
            raise ImageBuildError(f"base image Python is {info.python_version!r}, expected 3.12.x")
        return info

    def build(self, base_id: str, *, confirm: bool, lock_path: Path = LOCK_PATH) -> BuildResult:
        """Build the sandbox image. Must be explicitly confirmed by a human."""
        if not confirm:
            raise ImageBuildError("refusing to build without explicit confirmation (--confirm)")
        base = self.inspect_base(base_id)
        hashes = source_hashes(self.image_dir)
        args = [
            "build",
            "--pull=false",
            "--network", "none",
            "--no-cache",
            "--progress", "plain",
            "--build-arg", f"BASE_IMAGE={base.digest_ref}",
            "--label", f"org.agent-factory.sandbox.base={base.digest_ref}",
            "--label", f"org.agent-factory.sandbox.af-entry-sha256={hashes['af_entry_sha256']}",
            "--label", f"org.agent-factory.sandbox.dockerfile-sha256={hashes['dockerfile_sha256']}",
            "--tag", DEFAULT_IMAGE_REF,
            "--file", str(self.image_dir / "Dockerfile"),
            str(self.image_dir),
        ]  # fmt: skip
        out = self._docker(args, timeout_s=900)
        log = decode(out.stdout) + decode(out.stderr)
        if out.exit_code != 0:
            raise ImageBuildError(f"docker build failed (exit {out.exit_code}):\n{log[-3000:]}")
        lowered = log.lower()
        suspicious = [m for m in REGISTRY_ACCESS_MARKERS if m in lowered]
        if suspicious:
            raise ImageBuildError(f"build log suggests registry access ({suspicious}); not recording the image")

        built = self._inspect(DEFAULT_IMAGE_REF)
        if built is None or not IMAGE_ID_RE.match(str(built.get("Id", ""))):
            raise ImageBuildError("built image could not be inspected")
        config = built.get("Config") or {}
        if config.get("User") != f"{SANDBOX_UID}:{SANDBOX_UID}":
            raise ImageBuildError(f"built image user is {config.get('User')!r}")
        leaked = [e.split("=", 1)[0] for e in (config.get("Env") or []) if not e.startswith("PATH=")]
        if leaked:
            raise ImageBuildError(f"built image carries unexpected ENV {leaked}")
        labels = config.get("Labels") or {}
        if labels.get("org.agent-factory.sandbox.base") != base.digest_ref:
            raise ImageBuildError("built image does not carry the expected base label")

        record = ImageRecord(
            image_id=built["Id"],
            image_ref=DEFAULT_IMAGE_REF,
            base_image=base.digest_ref,
            base_image_id=base.image_id,
            base_python_version=base.python_version,
            dockerfile_sha256=hashes["dockerfile_sha256"],
            af_entry_sha256=hashes["af_entry_sha256"],
            built_at=timestamp(utc_now),
            docker_server_version=self.server_version,
        )
        save_image_record(record, lock_path)
        return BuildResult(record, log)
