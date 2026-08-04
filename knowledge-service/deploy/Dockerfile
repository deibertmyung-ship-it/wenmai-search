FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

# Server profile needs postgres + s3; docling/marker stay opt-in because they
# pull multi-GB model stacks. Add them here if you ingest PDFs.
RUN pip install --no-cache-dir ".[postgres,s3,fastembed]"

RUN useradd --create-home --uid 10001 kbsvc
USER kbsvc

EXPOSE 8077 8078

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8077/healthz', timeout=3).status==200 else 1)" || exit 1

CMD ["kbsvc", "serve", "--host", "0.0.0.0", "--port", "8077"]
