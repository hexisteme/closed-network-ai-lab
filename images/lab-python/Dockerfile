# The lab's Python image: python:3.12-slim plus the packages in requirements.lock.
# Packages enter only here, at build time, each checked against its pinned hash; the containers
# built from this image run on lab_internal, which has no route out.
# The lock was made with: uv pip compile requirements.in --generate-hashes --python-version 3.12
#   --python-platform aarch64-unknown-linux-gnu --exclude-newer 2026-10-08
FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --no-deps --require-hashes -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock

# No tracing client may try to reach a hosted service.
ENV LANGSMITH_TRACING=false \
    LANGCHAIN_TRACING_V2=false \
    PYTHONDONTWRITEBYTECODE=1
