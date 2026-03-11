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

# Copy source
COPY . .

# Create runtime directories and non-root user
RUN mkdir -p /app/uploads /app/data /app/.cache/huggingface
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

# Pre-download the sentence-transformers model as appuser so the cache is readable at runtime
ENV HF_HOME=/app/.cache/huggingface
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

EXPOSE 8000

# Single worker — the sentence-transformers model + FAISS index are kept in
# process memory; multiple workers would each load their own copy and quickly
# exhaust RAM even on the Standard 2 GB plan.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--timeout", "300", "run:app"]
