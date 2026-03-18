from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any

import pandas as pd
from openai import OpenAI

from services.openai_vision import extract_items_from_image as legacy_extract

logger = logging.getLogger(__name__)


_STAGE_A_PROMPT = (
    "Read this contractor material list exactly as written. "
    "Return strict JSON only as an array named lines. Preserve order and hierarchy. "
    "Identify probable section headers, child items, accessories, and notes. "
    "Extract quantities and dimensions but do not over-infer missing details. "
    "Each line must include: line_id, raw_text, section_header, section_type, quantity_raw, quantity, "
    "dimensions_raw, length, width, height, unit, indentation_level, bullet_style, source_page, source_order, "
    "confidence, unresolved_tokens."
)


class VisionExtractService:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def extract(self, file_path: Path) -> list[dict[str, Any]]:
        ext = file_path.suffix.lower()
        if ext == ".csv":
            return self._extract_csv(file_path)

        if not self.api_key:
            return self._legacy_extract(file_path)

        client = OpenAI(api_key=self.api_key)
        content = self._build_content(file_path)
        try:
            response = client.responses.create(
                model=self.model,
                input=[{"role": "user", "content": [{"type": "input_text", "text": _STAGE_A_PROMPT}, content]}],
            )
            parsed = _extract_json(response.output_text)
            lines = parsed.get("lines", parsed if isinstance(parsed, list) else [])
            normalized = [self._normalize_line(idx, line) for idx, line in enumerate(lines, start=1)]
            return [line for line in normalized if line["raw_text"]]
        except Exception:
            logger.exception("Stage A extraction failed, falling back to legacy extraction")
            return self._legacy_extract(file_path)

    def _legacy_extract(self, file_path: Path) -> list[dict[str, Any]]:
        items = legacy_extract(file_path, api_key=self.api_key, model=self.model)
        out: list[dict[str, Any]] = []
        for idx, item in enumerate(items, start=1):
            out.append(
                {
                    "line_id": f"L{idx}",
                    "raw_text": item.get("description", ""),
                    "section_header": "",
                    "section_type": "item",
                    "quantity_raw": str(item.get("quantity", "1")),
                    "quantity": float(item.get("quantity", 1) or 1),
                    "dimensions_raw": "",
                    "length": "",
                    "width": "",
                    "height": "",
                    "unit": "",
                    "indentation_level": 0,
                    "bullet_style": "",
                    "source_page": 1,
                    "source_order": idx,
                    "confidence": 0.6,
                    "unresolved_tokens": [],
                }
            )
        return out

    def _extract_csv(self, file_path: Path) -> list[dict[str, Any]]:
        df = pd.read_csv(file_path)
        lower_cols = {c.lower(): c for c in df.columns}
        description_col = next((lower_cols[c] for c in ("description", "item", "material", "name") if c in lower_cols), None)
        if not description_col:
            raise ValueError("CSV must contain a description-like column")
        quantity_col = next((lower_cols[c] for c in ("quantity", "qty", "count") if c in lower_cols), None)

        rows: list[dict[str, Any]] = []
        for idx, (_, row) in enumerate(df.iterrows(), start=1):
            raw_text = str(row.get(description_col, "")).strip()
            if not raw_text or raw_text.lower() == "nan":
                continue
            quantity = 1.0
            if quantity_col:
                try:
                    quantity = float(row.get(quantity_col, 1) or 1)
                except (TypeError, ValueError):
                    quantity = 1.0
            rows.append(
                {
                    "line_id": f"L{idx}",
                    "raw_text": raw_text,
                    "section_header": "",
                    "section_type": "item",
                    "quantity_raw": str(quantity),
                    "quantity": quantity,
                    "dimensions_raw": "",
                    "length": "",
                    "width": "",
                    "height": "",
                    "unit": "",
                    "indentation_level": 0,
                    "bullet_style": "",
                    "source_page": 1,
                    "source_order": idx,
                    "confidence": 0.95,
                    "unresolved_tokens": [],
                }
            )
        return rows

    def _build_content(self, file_path: Path) -> dict[str, str]:
        raw = file_path.read_bytes()
        b64 = base64.b64encode(raw).decode("utf-8")
        if file_path.suffix.lower() == ".pdf":
            return {
                "type": "input_file",
                "filename": file_path.name,
                "file_data": f"data:application/pdf;base64,{b64}",
            }
        mime = "image/jpeg" if file_path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
        return {"type": "input_image", "image_url": f"data:{mime};base64,{b64}"}

    def _normalize_line(self, idx: int, line: dict[str, Any]) -> dict[str, Any]:
        return {
            "line_id": str(line.get("line_id") or f"L{idx}"),
            "raw_text": str(line.get("raw_text", "")).strip(),
            "section_header": str(line.get("section_header", "")).strip(),
            "section_type": str(line.get("section_type", "unknown")),
            "quantity_raw": str(line.get("quantity_raw", "")),
            "quantity": float(line.get("quantity", 1) or 1),
            "dimensions_raw": str(line.get("dimensions_raw", "")),
            "length": str(line.get("length", "")),
            "width": str(line.get("width", "")),
            "height": str(line.get("height", "")),
            "unit": str(line.get("unit", "")),
            "indentation_level": int(line.get("indentation_level", 0) or 0),
            "bullet_style": str(line.get("bullet_style", "")),
            "source_page": int(line.get("source_page", 1) or 1),
            "source_order": int(line.get("source_order", idx) or idx),
            "confidence": float(line.get("confidence", 0.0) or 0.0),
            "unresolved_tokens": list(line.get("unresolved_tokens", []) or []),
        }


def _extract_json(text: str) -> Any:
    stripped = re.sub(r"^```(?:json)?\s*", "", text.strip())
    stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", stripped)
        if not match:
            raise
        return json.loads(match.group(1))
