from __future__ import annotations

import base64
import json
import logging
import re
from fractions import Fraction
from pathlib import Path
from typing import Any

import pandas as pd
from openai import OpenAI
from app.services.upload_context import normalize_document_context

logger = logging.getLogger(__name__)

_WORKFLOW_CONTEXT = (
    "These uploads are usually customer or competitor material lists in handwritten, typed, or mixed formats. "
    "Treat all provided pages/images as one document set when more than one file is supplied. "
    "Pay attention to the full context of the list because some rows are shorthand and inherit important details "
    "from headings, notes, legends, side annotations, or nearby pages. "
    "Preserve uncertainty instead of guessing, but do apply shared context when it is clearly supported by the document."
)


class VisionExtractService:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def extract(self, file_path: Path, upload_context: str = "") -> list[dict[str, Any]]:
        return self.extract_document([file_path], upload_context=upload_context)["lines"]

    def extract_many(self, file_paths: list[Path], upload_context: str = "") -> list[dict[str, Any]]:
        return self.extract_document(file_paths, upload_context=upload_context)["lines"]

    def extract_document(self, file_paths: list[Path], upload_context: str = "") -> dict[str, Any]:
        if not file_paths:
            return {"document_context": _empty_document_context(), "lines": []}

        if all(path.suffix.lower() == ".csv" for path in file_paths):
            combined: list[dict[str, Any]] = []
            for file_index, path in enumerate(file_paths, start=1):
                for row in self._extract_csv(path):
                    normalized = self._normalize_line(len(combined) + 1, row, file_index=file_index)
                    if normalized["raw_text"]:
                        combined.append(normalized)
            return {"document_context": _empty_document_context(), "lines": combined}

        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured for image/pdf parsing.")

        client = OpenAI(api_key=self.api_key)
        try:
            parsed = self._extract_document_payload(
                client,
                file_paths,
                upload_context=upload_context,
            )
            document_context = normalize_document_context(parsed.get("document_context"))
            raw_lines = parsed.get("lines", [])

            if len(file_paths) > 1 and not self._lines_have_valid_file_indexes(raw_lines, len(file_paths)):
                logger.warning(
                    "Stage A returned invalid file indexes for multi-file batch; retrying per file"
                )
                fallback_lines: list[dict[str, Any]] = []
                fallback_contexts: list[dict[str, Any]] = []
                for file_index, path in enumerate(file_paths, start=1):
                    single_parsed = self._extract_document_payload(
                        client,
                        [path],
                        upload_context=upload_context,
                    )
                    fallback_contexts.append(single_parsed.get("document_context") or {})
                    single_lines = single_parsed.get("lines", [])
                    single_normalized = [
                        self._normalize_line(idx, line, file_index=file_index)
                        for idx, line in enumerate(single_lines, start=1)
                    ]
                    fallback_lines.extend(
                        line for line in single_normalized if line["raw_text"]
                    )
                if not any(document_context.values()):
                    document_context = _merge_document_contexts(fallback_contexts)
                normalized = fallback_lines
            else:
                normalized = [
                    self._normalize_line(idx, line)
                    for idx, line in enumerate(raw_lines, start=1)
                ]
                normalized = [line for line in normalized if line["raw_text"]]

            return {"document_context": document_context, "lines": normalized}
        except Exception as exc:
            logger.exception("Stage A extraction failed")
            raise RuntimeError(f"Stage A extraction failed: {exc}") from exc

    def extract_legacy_items(self, file_path: Path, upload_context: str = "") -> list[dict[str, Any]]:
        if file_path.suffix.lower() == ".csv":
            return [{"quantity": row["quantity"], "description": row["raw_text"]} for row in self._extract_csv(file_path)]
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured for image/pdf parsing.")

        client = OpenAI(api_key=self.api_key)
        content = self._build_content(file_path)
        response = client.responses.create(
            model=self.model,
            input=[{"role": "user", "content": [{"type": "input_text", "text": self._build_legacy_prompt(upload_context=upload_context)}, content]}],
        )
        parsed = _extract_json(response.output_text)
        rows = parsed if isinstance(parsed, list) else parsed.get("items", [])
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            desc = str(row.get("description", "")).strip()
            if not desc:
                continue
            try:
                qty = float(row.get("quantity", 1) or 1)
            except (TypeError, ValueError):
                qty = 1.0
            out.append({"quantity": qty, "description": desc})
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
        mime_map = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }
        mime = mime_map.get(file_path.suffix.lower(), "image/png")
        return {"type": "input_image", "image_url": f"data:{mime};base64,{b64}"}

    def _build_multi_content(self, file_paths: list[Path]) -> list[dict[str, str]]:
        content: list[dict[str, str]] = []
        for file_index, path in enumerate(file_paths, start=1):
            content.append({"type": "input_text", "text": f"File {file_index}: {path.name}"})
            content.append(self._build_content(path))
        return content

    def _build_stage_a_prompt(self, file_count: int, upload_context: str = "") -> str:
        prompt = (
            f"{_WORKFLOW_CONTEXT} "
            f"You are extracting raw lines from {'multiple related uploads' if file_count > 1 else 'a single uploaded list'}. "
            "Read the material list exactly as written. "
            "Return strict JSON only as an object with keys document_context and lines. "
            'document_context must have keys summary, customer_name, project_name, global_material_context, job_notes, warnings. '
            "Preserve order, hierarchy, and note lines. "
            "Identify probable section headers, child items, accessories, carry-down notes, and annotations. "
            "Extract quantities and dimensions but do not over-infer missing details. "
            "When a row inherits brand, color, material, or product-family context from a heading or general note, "
            "keep the raw row text intact and capture that relationship through section_header and unresolved_tokens rather than silently rewriting the row. "
            f"When more than one file is provided, every line must include file_index as an integer from 1 to {file_count}. "
            "Each line must include: file_index, line_id, raw_text, section_header, section_type, quantity_raw, quantity, "
            "dimensions_raw, length, width, height, unit, indentation_level, bullet_style, source_page, source_order, "
            "confidence, unresolved_tokens."
        )
        upload_context = " ".join(upload_context.split())
        if upload_context:
            prompt += f" User-provided upload context: {upload_context}."
        return prompt

    def _extract_document_payload(
        self,
        client: OpenAI,
        file_paths: list[Path],
        upload_context: str = "",
    ) -> dict[str, Any]:
        content = self._build_multi_content(file_paths)
        prompt = self._build_stage_a_prompt(
            file_count=len(file_paths),
            upload_context=upload_context,
        )
        response = client.responses.create(
            model=self.model,
            input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}, *content]}],
        )
        parsed = _extract_json(response.output_text)
        if isinstance(parsed, list):
            parsed = {"document_context": _empty_document_context(), "lines": parsed}
        elif not isinstance(parsed, dict):
            parsed = {"document_context": _empty_document_context(), "lines": []}
        return {
            "document_context": normalize_document_context(parsed.get("document_context")),
            "lines": parsed.get("lines", []),
        }

    def _lines_have_valid_file_indexes(self, lines: list[dict[str, Any]], file_count: int) -> bool:
        if file_count <= 1:
            return True
        if not lines:
            return True
        for line in lines:
            file_index = line.get("file_index")
            if not isinstance(file_index, int):
                return False
            if file_index < 1 or file_index > file_count:
                return False
        return True

    def _build_legacy_prompt(self, upload_context: str = "") -> str:
        prompt = (
            f"{_WORKFLOW_CONTEXT} "
            "Extract each line item with quantity and description. "
            "Return ONLY a JSON array with no surrounding markdown. "
            "Each element must have quantity (number) and description (string). "
            "Use quantity=1 when missing and preserve material measurements/sizes."
        )
        upload_context = " ".join(upload_context.split())
        if upload_context:
            prompt += f" User-provided upload context: {upload_context}."
        return prompt

    def _normalize_line(self, idx: int, line: dict[str, Any], file_index: int | None = None) -> dict[str, Any]:
        return {
            "file_index": int(line.get("file_index", file_index or 1) or 1),
            "line_id": str(line.get("line_id") or f"L{idx}"),
            "raw_text": str(line.get("raw_text", "")).strip(),
            "section_header": str(line.get("section_header", "")).strip(),
            "section_type": str(line.get("section_type", "unknown")),
            "quantity_raw": str(line.get("quantity_raw", "")),
            "quantity": _coerce_quantity(line.get("quantity", 1)),
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


def _empty_document_context() -> dict[str, Any]:
    return normalize_document_context({})


def _merge_document_contexts(contexts: list[dict[str, Any]]) -> dict[str, Any]:
    merged = _empty_document_context()
    for context in contexts:
        current = normalize_document_context(context)
        if current["summary"] and not merged["summary"]:
            merged["summary"] = current["summary"]
        if current["customer_name"] and not merged["customer_name"]:
            merged["customer_name"] = current["customer_name"]
        if current["project_name"] and not merged["project_name"]:
            merged["project_name"] = current["project_name"]
        for key in ("global_material_context", "job_notes", "warnings"):
            for value in current[key]:
                if value not in merged[key]:
                    merged[key].append(value)
    return merged


def _coerce_quantity(value: Any) -> float:
    if value in (None, ""):
        return 1.0
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return 1.0

    normalized = text.replace(",", "")
    if re.fullmatch(r"\d+(?:\.\d+)?", normalized):
        return float(normalized)

    if re.fullmatch(r"\d+\s+\d+/\d+", normalized):
        whole, frac = normalized.split(None, 1)
        return float(int(whole) + Fraction(frac))

    if re.fullmatch(r"\d+/\d+", normalized):
        return float(Fraction(normalized))

    return 1.0
