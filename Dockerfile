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

# Copy pre-built Python packages from builder stage
COPY --from=builder /install /usr/local

# Copy application source
COPY app/ ./app/

# ── Playwright browser installation ──────────────────────────────────────────
# Pin browsers to a fixed, world-readable path so the non-root user can use
# them at runtime without needing access to /root/.cache.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# 1. Install all OS-level dependencies that Chromium requires.
#    `playwright install-deps chromium` is the official method and handles
#    every distro-specific package automatically.
RUN playwright install-deps chromium

# 2. Download the Chromium browser binary into /ms-playwright.
RUN playwright install chromium

# ── Non-root user for security ────────────────────────────────────────────────
# Create the user AFTER browser installation (browsers live in /ms-playwright,
# not in /root/.cache, so they are accessible to any user).
RUN useradd --create-home appuser \
    && chown -R appuser:appuser /ms-playwright

USER appuser

# Expose the application port
EXPOSE 8000

# Launch the API server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
