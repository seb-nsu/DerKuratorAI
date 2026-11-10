# DerKurator AI – one image for every front end; FRONTEND (streamlit | nicegui | react) picks the one that serves.
#   docker build -t derkurator .
#   docker run -p 8080:8080 --env-file .env -v derkurator-data:/app/data derkurator
# Secrets (API keys, AUTH_*, API_TOKEN_SECRET, NICEGUI_STORAGE_SECRET) come from the environment, never the image.

FROM node:22-slim AS react
WORKDIR /build
COPY frontends/design ./design
COPY frontends/react/package.json frontends/react/package-lock.json ./react/
RUN cd react && npm ci --no-audit --no-fund
COPY frontends/react ./react
RUN cd react && npm run build

FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    FRONTEND=streamlit HOST=0.0.0.0 PORT=8080 ENVIRONMENT=PROD
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY frontends ./frontends
COPY scripts ./scripts
COPY data ./data
COPY .streamlit ./.streamlit
COPY --from=react /build/react/dist ./frontends/react/dist
RUN useradd --create-home --uid 10001 kurator && chown -R kurator /app/data
USER kurator
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request; p='/_stcore/health' if os.environ['FRONTEND'].strip().lower()=='streamlit' else '/api/v1/health'; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}{p}', timeout=4)" || exit 1
CMD ["python", "scripts/serve.py"]
