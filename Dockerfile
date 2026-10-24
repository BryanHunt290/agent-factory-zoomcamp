# syntax=docker/dockerfile:1
FROM node:24.18.0-bookworm-slim@sha256:6f7b03f7c2c8e2e784dcf9295400527b9b1270fd37b7e9a7285cf83b6951452d AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
COPY openapi.yaml /build/openapi.yaml
RUN npm run build

FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 AWS_EC2_METADATA_DISABLED=true
RUN apt-get update && apt-get install -y --no-install-recommends git libstdc++6 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home app
COPY --from=frontend /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY requirements-*.txt ./
RUN python -m pip install --no-cache-dir -r requirements-dev.txt -r requirements-api.txt \
    -r requirements-postgres.txt -r requirements-browser.txt -r requirements-aws.txt \
    && python -m pip check && node --version
COPY agent_factory/ ./agent_factory/
COPY scripts/reviewer.py scripts/docker_runtime.py ./scripts/
COPY openapi.yaml ./
COPY --from=frontend /build/frontend ./frontend/
RUN mkdir /state /workspaces /credentials /database \
    && chown app:app /state /workspaces /credentials
USER 10001:10001
EXPOSE 14173
ENTRYPOINT ["python", "scripts/docker_runtime.py"]
CMD ["serve"]
