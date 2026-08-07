FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Native runtimes used by Unstructured/Marker for MIME detection, OCR and PDF
# rendering. First, above every COPY: this layer depends only on the base image,
# so editing pyproject.toml can never invalidate it. It used to sit below the
# manifest copy, which meant adding one optional extra re-ran apt against a
# flaky mirror and failed the whole build.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        libglib2.0-0 \
        libgl1 \
        libmagic1 \
        poppler-utils \
        tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Dependencies are resolved from the manifest alone, before any source is
# copied. `pip install .` needs an importable package to build metadata from,
# so a stub stands in for the real one - it is overwritten below. Without this
# split, editing a single line of kbsvc invalidates every layer underneath and
# re-downloads torch.
COPY pyproject.toml README.md ./
RUN mkdir -p src/kbsvc && touch src/kbsvc/__init__.py

# CPU-only torch, ahead of everything that depends on it. The default Linux
# wheels pull ~5 GB of nvidia CUDA libraries that this stack can never use -
# there is no GPU in the compose topology - and downloading them is the largest
# single contributor to build time, image size and peak memory.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
    torch torchvision

# Docling, Unstructured and Marker are core dependencies. Kept in their own
# layer: on a memory-constrained host the resolver is the step most likely to
# be killed, and a separate layer means a retry resumes from cache instead of
# re-downloading torch.
RUN pip install --no-cache-dir ".[postgres,s3,fastembed,plagiarism]"

# Only now the real source, and a metadata-only reinstall over the stub. Every
# layer above is cached across source edits; this one costs a second.
COPY src ./src
RUN pip install --no-cache-dir --no-deps --force-reinstall .

# Import the parsers during the build so a deployment fails here rather than
# accepting jobs it cannot process.
RUN python -c "from docling.document_converter import DocumentConverter; from marker.converters.pdf import PdfConverter; from marker.models import create_model_dict; from marker.output import text_from_rendered; from unstructured.partition.auto import partition"

RUN useradd --create-home --uid 10001 kbsvc

# KB_DATA_DIR in the server profile, and the mount point for the shared volume
# holding the lexical index and the ONNX weights. Docker seeds a fresh named
# volume with the ownership of the image directory it covers, so creating it as
# kbsvc here is what lets the unprivileged user write to it.
RUN mkdir -p /data && chown kbsvc:kbsvc /data

USER kbsvc

EXPOSE 8077 8078

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8077/healthz', timeout=3).status==200 else 1)" || exit 1

CMD ["kbsvc", "serve", "--host", "0.0.0.0", "--port", "8077"]
