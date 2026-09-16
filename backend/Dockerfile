# Single-stage build: simple and easy to read over a marginally smaller
# image — this is a small FastAPI service, not something where multi-stage
# build complexity earns its keep.
FROM python:3.13-slim

WORKDIR /app

# requirements.txt copied and installed before the rest of the source, so
# Docker's layer cache only reinstalls dependencies when requirements.txt
# actually changes, not on every code edit.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only the actual application package is copied in — not simulator.py
# (meant to run locally against the deployed backend, per README), not
# tests/ (those run in CI/locally, not inside the deployed container).
COPY app ./app

# Render (and most PaaS hosts) assign the port to bind via $PORT at
# runtime, which is why this is CMD's shell form (for env var expansion)
# rather than exec form. 8000 is the fallback for `docker run` parity
# with the rest of this project's local-dev instructions.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
