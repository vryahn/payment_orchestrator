FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
# uvicorn lives in requirements-dev.txt (the offline/dev stack: duckdb, pandas,
# pyarrow, uvicorn, httpx), not requirements.txt -- Vercel never installs it,
# because the deployed function needs no ASGI server of its own. A container
# does, so it's added here as one pinned package instead of pulling in
# requirements-dev.txt wholesale, which would drag duckdb/pandas/pyarrow (only
# needed to rebuild the offline tables, never to serve) into the image.
RUN pip install --no-cache-dir -r requirements.txt uvicorn==0.41.0

COPY . .

# Default process is the API. The outbox drain worker overrides this CMD
# rather than getting a second Dockerfile:
#   docker run --rm orchestrator python worker.py
CMD ["uvicorn", "api.index:app", "--host", "0.0.0.0", "--port", "8000"]
