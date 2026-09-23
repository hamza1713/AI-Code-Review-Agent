# Build explicitly, review the resulting image, and configure its immutable image ID.
FROM python:3.12-slim
RUN pip install --no-cache-dir pytest==8.4.2
USER 65534:65534
WORKDIR /workspace
