FROM python:3.11-slim-bookworm

# Dependencias minimas de sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalar Chromium do Playwright com TODAS as dependencias de sistema
RUN playwright install --with-deps chromium

# Copiar codigo
COPY . .

# Criar diretorio para estado persistente do browser
RUN mkdir -p /tmp/hoteldo-browser-state

# Porta padrao (Railway injeta PORT)
ENV PORT=8000
EXPOSE 8000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:${PORT}/health || exit 1

# Entrypoint — exec form com sh -c garante expansao de $PORT
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --log-level info"]
