FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY kbweb ./kbweb
COPY wsgi.py ./

# kbweb talks to kbsvc over HTTP only - no database driver, no vector client, no
# parser stack. That is the whole point of the split, and it keeps this image
# two orders of magnitude smaller than the service image.
RUN pip install --no-cache-dir ".[prod]"

RUN useradd --create-home --uid 10002 kbweb
USER kbweb

EXPOSE 5055

# A TCP probe, not a request to `/`: that page calls kbsvc, so an unhealthy
# backend would mark the frontend unhealthy and take down the error page that
# exists to explain the outage.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import socket,sys; s=socket.create_connection(('127.0.0.1',5055),3); s.close()" || exit 1

CMD ["waitress-serve", "--host", "0.0.0.0", "--port", "5055", "wsgi:app"]
