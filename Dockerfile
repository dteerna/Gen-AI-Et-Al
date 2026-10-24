# Small, non-root image. Runtime dependency: pypdf only (add requirements-azure.txt for Azure OpenAI).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt requirements-azure.txt ./
ARG WITH_AZURE=true
RUN if [ "$WITH_AZURE" = "true" ]; then pip install -r requirements-azure.txt; else pip install -r requirements.txt; fi

COPY glcc ./glcc
COPY data ./data
COPY config ./config

# SQLite lives here. In Azure Container Apps this is ephemeral unless you mount Azure Files (see docs/AZURE_DEPLOYMENT.md).
ENV GLCC_DB=/tmp/glcc.db GLCC_DATA=/app/data
RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "glcc", "serve", "--host", "0.0.0.0", "--port", "8000"]
