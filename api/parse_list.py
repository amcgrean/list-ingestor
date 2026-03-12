"""
Vercel Serverless Function: POST /api/parse-list
-------------------------------------------------
Receives an uploaded image, sends it to the OpenAI Vision API, extracts
material list items, matches them to SKUs, and returns structured JSON.

Endpoint: POST /api/parse-list

Request (multipart/form-data):
    image   — image file (JPG / PNG / WebP)

Request (application/json):
    { "image_url": "https://..." }

Response 200:
    [
      {
        "quantity":    25,
        "input_text":  "2x10 joists 16ft",
        "matched_sku": "0210tre16",
        "confidence":  0.92
      },
      ...
    ]

Response 4xx / 5xx:
    { "error": "..." }

Environment variables required (set in Vercel project settings):
    OPENAI_API_KEY  — OpenAI key with access to gpt-4o or equivalent.
    DATABASE_URL    — Neon Postgres connection string.

Images are processed in-memory or via a short-lived temp file and are
never stored permanently, satisfying the security requirement.
"""

from __future__ import annotations

import email
import email.policy
import io
import json
import logging
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler

# ---------------------------------------------------------------------------
# Make project root importable regardless of Vercel's working directory
# ---------------------------------------------------------------------------
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from services.list_parser import parse_and_match  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Vercel functions must complete within ~10 seconds; gpt-4o typically responds
# in 3-6 s for a single image, leaving margin for DB queries.


class handler(BaseHTTPRequestHandler):
    """Vercel Python serverless handler for POST /api/parse-list."""

    def log_message(self, fmt: str, *args) -> None:  # type: ignore[override]
        """Route access logs through Python logging instead of stderr."""
        logger.info(fmt, *args)

    # ------------------------------------------------------------------
    # POST handler
    # ------------------------------------------------------------------

    def do_POST(self) -> None:
        try:
            result = self._handle()
            self._send_json(200, result)
        except _HttpError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except Exception as exc:
            logger.exception("Unhandled error in parse-list handler")
            self._send_json(500, {"error": f"Internal server error: {exc}"})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _handle(self) -> list[dict]:
        content_type = self.headers.get("content-type", "")
        content_length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(content_length)

        if "multipart/form-data" in content_type:
            return self._handle_multipart(content_type, body)
        elif "application/json" in content_type:
            return self._handle_json(body)
        else:
            raise _HttpError(
                415,
                "Unsupported content type. Use multipart/form-data or application/json.",
            )

    def _handle_multipart(self, content_type: str, body: bytes) -> list[dict]:
        """Parse multipart upload, save to temp file, run pipeline, delete."""
        raw_bytes = _extract_multipart_field(content_type, body, field="image")
        if raw_bytes is None:
            raise _HttpError(400, "No 'image' field in multipart body.")
        if len(raw_bytes) == 0:
            raise _HttpError(400, "Uploaded image is empty.")

        # Write to a short-lived temp file; deleted in finally block
        suffix = _detect_suffix(raw_bytes)
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(raw_bytes)
                tmp_path = tmp.name
            return parse_and_match(tmp_path)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _handle_json(self, body: bytes) -> list[dict]:
        """Accept { "image_url": "https://..." } payload."""
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise _HttpError(400, f"Invalid JSON: {exc}") from exc

        image_url = payload.get("image_url", "").strip()
        if not image_url:
            raise _HttpError(400, "JSON body must include 'image_url'.")
        if not (image_url.startswith("http://") or image_url.startswith("https://")):
            raise _HttpError(400, "'image_url' must be an http/https URL.")

        return parse_and_match(image_url)

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _extract_multipart_field(content_type: str, body: bytes, field: str) -> bytes | None:
    """Extract a single named field from a multipart/form-data body.

    Uses only stdlib ``email`` — no ``cgi`` module required.
    """
    # email.message_from_bytes needs headers prepended to the body
    raw = f"Content-Type: {content_type}\r\n\r\n".encode() + body
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    for part in msg.walk():
        disposition = part.get("Content-Disposition", "")
        # Look for name="image" (or name=image without quotes)
        if f'name="{field}"' in disposition or f"name={field}" in disposition:
            payload = part.get_payload(decode=True)
            return payload if isinstance(payload, bytes) else None
    return None


class _HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _detect_suffix(raw: bytes) -> str:
    """Guess image file suffix from magic bytes."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"  # safe default
