# Local bundle: runs inside a customer's network, no outbound calls needed.
# Build from this folder:  docker build -t laforge:0.1 .
FROM python:3.12-slim
RUN pip install --no-cache-dir "mcp>=1.2,<2" duckdb pandas pyarrow
COPY laforge /app/laforge
WORKDIR /app
ENV LAFORGE_SNAPSHOT_DIR=/data LAFORGE_CATALOG=/data/catalog.json LAFORGE_OUT_DIR=/out
CMD ["python", "-m", "laforge.server"]
