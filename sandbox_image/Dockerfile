# Agent Factory sandbox runtime image.
#
# Built ONLY by the human-run command:
#     python -m agent_factory sandbox build-image --base <sha256 digest> --confirm
# Agent runs can never build, pull, or update images.
#
# There is deliberately no "# syntax=" directive: the builder's built-in
# Dockerfile frontend is used, so no frontend image is fetched.
#
# BASE_IMAGE must be a digest-pinned reference to an image already present
# locally (e.g. docker.io/library/python@sha256:...). The build runs with
# --pull=false and --network none.

ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS base

# Start from an empty config: copying the base filesystem into "scratch"
# drops the base image's ENV (GPG_KEY, PYTHON_SHA256, ...), CMD and labels,
# so the container environment is exactly what DockerBackend passes.
FROM scratch
COPY --from=base / /
COPY af-entry /usr/local/bin/af-entry

# - af-entry: root-owned, read/execute only
# - strip every setuid/setgid bit from the image
# - numeric sandbox user 10001 (no home, no login shell)
# - /work exists as a mount point (replaced by a tmpfs at runtime)
RUN chown 0:0 /usr/local/bin/af-entry \
 && chmod 0555 /usr/local/bin/af-entry \
 && find / -xdev -type f -perm /6000 -exec chmod a-s {} + \
 && echo "sandbox:x:10001:10001:Agent Factory sandbox:/tmp:/usr/sbin/nologin" >> /etc/passwd \
 && echo "sandbox:x:10001:" >> /etc/group \
 && mkdir -p /work \
 && chown 10001:10001 /work \
 && chmod 0700 /work

USER 10001:10001
WORKDIR /work
LABEL org.agent-factory.sandbox.protocol="1"
