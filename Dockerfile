FROM python:3.11-slim

# Dependencias de sistema para o Playwright Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    xdg-utils \
    libxss1 \
    libxtst6 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf2.0-0 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalar browsers do Playwright (somente Chromium para economizar espaco)
RUN playwright install chromium
RUN playwright install-deps chromium

# Copiar codigo
COPY . .

# Criar diretorio para estado persistente do browser
RUN mkdir -p /tmp/hoteldo-browser-state

# Porta padrao (Railway injeta PORT)
ENV PORT=8000
EXPOSE ${PORT}

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:${PORT}/health || exit 1

# Entrypoint
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT} --log-level info
