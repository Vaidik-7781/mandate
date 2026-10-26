# Not built in the author's environment (no Docker daemon there): CI builds it on every push.
FROM node:22-slim AS web
WORKDIR /w
COPY apps/web/package*.json ./
RUN npm ci
COPY apps/web ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE alembic.ini ./
COPY apps/api apps/api
COPY alembic alembic
COPY bench bench
COPY spec spec
COPY fixtures fixtures
RUN pip install --no-cache-dir ".[postgres]"
COPY --from=web /w/dist apps/web/dist
ENV PYTHONPATH=/app/apps/api
RUN useradd -r -u 10001 mandate && chown -R mandate /app
USER mandate
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request as u,os;u.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health')" || exit 1
CMD ["sh", "-c", "alembic upgrade head && uvicorn mandate.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
