FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

# Native runtimes used by Unstructured/Marker for MIME detection, OCR and PDF rendering.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        libglib2.0-0 \
        libgl1 \
        libmagic1 \
        poppler-utils \
        tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Docling, Unstructured and Marker are core dependencies. Import them during the
# image build so a deployment fails early instead of accepting unprocessable jobs.
RUN pip install --no-cache-dir ".[postgres,s3,fastembed]" \
    && python -c "from docling.document_converter import DocumentConverter; from marker.converters.pdf import PdfConverter; from marker.models import create_model_dict; from marker.output import text_from_rendered; from unstructured.partition.auto import partition"

RUN useradd --create-home --uid 10001 kbsvc
USER kbsvc

EXPOSE 8077 8078

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8077/healthz', timeout=3).status==200 else 1)" || exit 1

CMD ["kbsvc", "serve", "--host", "0.0.0.0", "--port", "8077"]
