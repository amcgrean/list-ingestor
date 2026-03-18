# Hosting Decision: Raspberry Pi vs Vercel

**Decision: Stay on Raspberry Pi**

## Context

Evaluated whether to move back to Vercel following recent workflow changes
(Tesseract OCR replaced by OpenAI Vision API, multi-file uploads, review feedback loop).

## Why Pi wins

The app runs a single gunicorn worker intentionally — it keeps `sentence-transformers`
(~200 MB) and the FAISS vector index warm in shared memory across requests. This
state can't be replicated on Vercel's serverless model:

- Cold starts would reload the model on every invocation
- The 1.5 GB Docker image can't run as a Vercel function
- 10–30s function timeouts are too tight for cold start + processing
- No shared state across serverless instances

## Impact of the OCR switch

Removing Tesseract actually **reduced** local CPU requirements. The Pi now:

- Offloads OCR to OpenAI Vision API (external, ~5s network call)
- Handles embedding generation (~500ms), FAISS search (<100ms), and DB queries locally
- Does less per-upload than before

## Pi resource requirements

- **RAM**: ~250–300 MB idle, up to ~1 GB under load (Pi 3B+ or 4 is fine)
- **CPU**: Low — matching and embedding are fast; no local image processing
- **Storage**: Minimal — temp files deleted immediately after processing

## When to revisit

Vercel becomes viable only if sentence-transformers/FAISS is replaced with a
managed vector DB (e.g. Supabase pgvector, Pinecone). Not worth the refactor
for current usage volume.

If cloud hosting is ever needed, **Fly.io** is the closest equivalent to the Pi
setup (`fly.toml` already configured): scale-to-zero, 1 GB RAM, ~$5–10/month.
