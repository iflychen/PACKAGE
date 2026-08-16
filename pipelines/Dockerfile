FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN addgroup --system aniki \
    && adduser --system --ingroup aniki aniki

COPY --chown=aniki:aniki . ./
RUN mkdir -p /app/data \
    && chown -R aniki:aniki /app

USER aniki

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)"

CMD ["uvicorn", "aniki_api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
