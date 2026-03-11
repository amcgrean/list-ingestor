# ---- Build stage ----
FROM python:3.12-slim AS base

# System dependencies: Tesseract OCR + Poppler (for pdf2image)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    poppler-utils \
    libpq-dev \
    gcc \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies before copying source (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the sentence-transformers model into the image
# (avoids a slow download on first request in production)
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Copy source
COPY . .

# Create runtime directories
RUN mkdir -p /app/uploads /app/data

# Non-root user for security
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Use gunicorn in production.
# WEB_CONCURRENCY is set to 1 by Render on the free/starter plan (512 MB RAM).
# sentence-transformers + PyTorch require ~300 MB, so a single worker is required
# to stay within the memory limit.  Override WEB_CONCURRENCY in the Render
# dashboard if you upgrade to an instance with more RAM.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:8000 --workers ${WEB_CONCURRENCY:-1} --timeout 120 run:app"]
