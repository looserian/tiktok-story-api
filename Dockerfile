# ─────────────────────────────────────────────
# Stage 1: Builder — install Python deps
# ─────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build tools needed by some Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ─────────────────────────────────────────────
# Stage 2: Runtime — lean final image
# ─────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Install Playwright system dependencies (Chromium browser)
# playwright install-deps injects all OS-level packages required
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Core Chromium dependencies
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libdbus-1-3 libxkbcommon0 libx11-6 libxcomposite1 \
    libxdamage1 libxext6 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libatspi2.0-0 libxcb1 fonts-liberation \
    # Additional utilities
    wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy pre-built Python packages from builder stage
COPY --from=builder /install /usr/local

# Copy application source
COPY app/ ./app/

# Install Playwright browsers (Chromium only — smallest footprint)
RUN playwright install chromium

# Expose the application port
EXPOSE 8000

# Non-root user for security
RUN useradd --create-home appuser
USER appuser

# Launch the API server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
