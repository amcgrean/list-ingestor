"""
OpenAI Vision Service
---------------------
Sends an uploaded image to the OpenAI Responses API and extracts structured
material-list items plus document-level context.

Usage::

    from services.openai_vision import extract_items_from_image

    items = extract_items_from_image("/path/to/photo.jpg", api_key="sk-...")
    # [{"quantity": 25, "description": "2x10 joists 16ft"}, ...]

Accepts a local file path, a public image URL, or raw bytes.
Images are base64-encoded and sent inline and are never stored.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Iterable, Union

from openai import OpenAI

logger = logging.getLogger(__name__)

_BASE_SYSTEM_PROMPT = (
    "You are reading a customer or competitor material list from photos or PDFs. "
    "These lists may be handwritten, typed, or mixed. "
    "Extract each material line with quantity and a fully qualified description. "
    "Apply document-level context when it clearly governs later rows, including headings, general notes, side annotations, and later or earlier pages in the same upload set. "
    "For example, if a heading says 'Trex Toasted Sand' and the rows below list generic sizes, "
    "include the Trex/Toasted Sand context in those line-item descriptions unless a later row overrides it. "
    "Also capture customer names, job names, and job notes when present. "
    "Return ONLY a JSON object with this exact shape and no markdown: "
    '{"document_context":{"summary":"","customer_name":"","project_name":"","global_material_context":[],"job_notes":[],"warnings":[]},'
    '"items":[{"quantity":1,"description":"","source_description":"","applied_context":[]}]} '
    "Use description for the best matching-ready text. "
    "Use source_description for the original wording when different. "
    "Do not invent line items or quantities."
)

_EXT_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def extract_document_data_from_image(
    image_source: Union[str, Path, bytes],
    api_key: str,
    model: str = "gpt-4o",
    upload_context: str = "",
) -> dict:
    """Send an image to the OpenAI Responses API and return items plus context."""
    return extract_document_data_from_images(
        [image_source],
        api_key=api_key,
        model=model,
        upload_context=upload_context,
    )


def extract_document_data_from_images(
    image_sources: Iterable[Union[str, Path, bytes]],
    api_key: str,
    model: str = "gpt-4o",
    upload_context: str = "",
) -> dict:
    """Send one or more images/PDFs to the OpenAI Responses API and return items plus context."""
    if not api_key:
        raise ValueError("api_key must not be empty")

    client = OpenAI(api_key=api_key)
    image_sources = list(image_sources)
    content = _build_image_contents(image_sources)
    prompt = _build_system_prompt(upload_context=upload_context, file_count=len(image_sources))

    try:
        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        *content,
                    ],
                }
            ],
        )
        raw_text = response.output_text.strip()
    except Exception as exc:
        logger.exception("OpenAI Responses API call failed")
        raise RuntimeError(f"Vision API error: {exc}") from exc

    payload = _parse_json_response(raw_text)
    logger.info("vision_extracted items=%d", len(payload["items"]))
    return payload


def extract_items_from_image(
    image_source: Union[str, Path, bytes],
    api_key: str,
    model: str = "gpt-4o",
    upload_context: str = "",
) -> list[dict]:
    """Backward-compatible helper that returns only extracted items."""
    payload = extract_document_data_from_image(
        image_source,
        api_key=api_key,
        model=model,
        upload_context=upload_context,
    )
    return payload["items"]


def _build_image_content(image_source: Union[str, Path, bytes]) -> dict:
    """Return a Responses API input_image content block."""
    if isinstance(image_source, bytes):
        b64 = base64.b64encode(image_source).decode("utf-8")
        return {
            "type": "input_image",
            "image_url": f"data:image/jpeg;base64,{b64}",
        }

    source_str = str(image_source)
    if source_str.startswith("http://") or source_str.startswith("https://"):
        return {
            "type": "input_image",
            "image_url": source_str,
        }

    path = Path(image_source)
    mime = _EXT_TO_MIME.get(path.suffix.lower(), "image/jpeg")
    with open(path, "rb") as fh:
        raw = fh.read()
    b64 = base64.b64encode(raw).decode("utf-8")
    return {
        "type": "input_image",
        "image_url": f"data:{mime};base64,{b64}",
    }


def _build_image_contents(image_sources: list[Union[str, Path, bytes]]) -> list[dict]:
    content: list[dict] = []
    for idx, image_source in enumerate(image_sources, start=1):
        if isinstance(image_source, (str, Path)):
            label = Path(str(image_source)).name
        else:
            label = f"inline-{idx}.jpg"
        content.append({"type": "input_text", "text": f"File {idx}: {label}"})
        content.append(_build_image_content(image_source))
    return content


def _build_system_prompt(upload_context: str = "", file_count: int = 1) -> str:
    prompt = _BASE_SYSTEM_PROMPT
    if file_count > 1:
        prompt += " Treat all provided files as one related document set and resolve sparse rows using the full set when clearly supported."
    upload_context = " ".join(str(upload_context or "").split())
    if upload_context:
        prompt += f" User-provided upload context: {upload_context}."
    return prompt


def _parse_json_response(text: str) -> dict:
    """Extract and validate structured JSON from the model response."""
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
    text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = _extract_embedded_json(text)
        if data is None:
            logger.error("No JSON payload found in vision response: %.200s", text)
            return {"document_context": _empty_document_context(), "items": []}

    if isinstance(data, list):
        raw_context = {}
        raw_items = data
    elif isinstance(data, dict):
        raw_context = data.get("document_context", {})
        raw_items = data.get("items", [])
    else:
        logger.error("Expected JSON object/array from vision, got %s", type(data).__name__)
        return {"document_context": _empty_document_context(), "items": []}

    validated: list[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description", "")).strip()
        if not description:
            continue
        try:
            quantity = float(item.get("quantity", 1))
        except (TypeError, ValueError):
            quantity = 1.0

        source_description = str(item.get("source_description", "")).strip() or description
        applied_context = item.get("applied_context", [])
        if isinstance(applied_context, str):
            applied_context = [applied_context]
        cleaned_context = [str(value).strip() for value in applied_context if str(value).strip()]

        validated.append(
            {
                "quantity": quantity,
                "description": description,
                "source_description": source_description,
                "applied_context": cleaned_context,
            }
        )

    return {
        "document_context": _normalize_document_context(raw_context),
        "items": validated,
    }


def _extract_embedded_json(text: str) -> object | None:
    for pattern in (r"\{.*\}", r"\[.*\]"):
        match = re.search(pattern, text, re.DOTALL)
        if not match:
            continue
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            continue
    return None


def _empty_document_context() -> dict:
    return {
        "summary": "",
        "customer_name": "",
        "project_name": "",
        "global_material_context": [],
        "job_notes": [],
        "warnings": [],
    }


def _normalize_document_context(data: object) -> dict:
    if not isinstance(data, dict):
        return _empty_document_context()

    def _text(value: object) -> str:
        return " ".join(str(value or "").split())

    def _list(value: object) -> list[str]:
        if not value:
            return []
        if isinstance(value, str):
            value = [value]
        cleaned: list[str] = []
        for item in value:
            text = _text(item)
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned

    return {
        "summary": _text(data.get("summary")),
        "customer_name": _text(data.get("customer_name")),
        "project_name": _text(data.get("project_name")),
        "global_material_context": _list(data.get("global_material_context")),
        "job_notes": _list(data.get("job_notes")),
        "warnings": _list(data.get("warnings")),
    }
