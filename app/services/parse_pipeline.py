from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from flask import current_app

from app.services.context_interpreter import ContextInterpreter
from app.services.parse_types import ContextualizedLine, MatchReadyLine, RawExtractedLine
from app.services.vision_extract_service import VisionExtractService

logger = logging.getLogger(__name__)


def parse_uploads(
    upload_paths: list[Path],
    api_key: str,
    session_id: int | None = None,
    upload_context: str = "",
) -> tuple[list[RawExtractedLine], list[ContextualizedLine], list[MatchReadyLine], dict[str, Any]]:
    vision_service = VisionExtractService(api_key=api_key, model=current_app.config["OPENAI_EXTRACTION_MODEL"])
    interpreter = ContextInterpreter(api_key=api_key, model=current_app.config["OPENAI_CONTEXT_MODEL"])

    stage_a, stage_a_context = stage_a_extract(upload_paths, vision_service, upload_context=upload_context)
    stage_b = stage_b_interpret(stage_a, interpreter, upload_context=upload_context)
    stage_c = stage_c_prepare_for_matching(stage_b)

    if current_app.config.get("PARSE_DEBUG_SAVE_JSON"):
        _save_debug_artifacts(session_id, stage_a, stage_b, stage_c)

    return stage_a, stage_b, stage_c, stage_a_context


def stage_a_extract(
    upload_paths: list[Path],
    vision_service: VisionExtractService,
    upload_context: str = "",
) -> tuple[list[RawExtractedLine], dict[str, Any]]:
    lines: list[RawExtractedLine] = []
    order = 0
    merged_context: dict[str, Any] = {}
    visual_entries = [
        (original_index, path)
        for original_index, path in enumerate(upload_paths, start=1)
        if path.suffix.lower() != ".csv"
    ]
    extracted_by_file: dict[int, list[dict[str, Any]]] = {}

    if visual_entries:
        extracted_document = vision_service.extract_document(
            [path for _, path in visual_entries],
            upload_context=upload_context,
        )
        extracted_visual = extracted_document.get("lines", [])
        merged_context = dict(extracted_document.get("document_context") or {})
        visual_index_map = {
            visual_position: original_index
            for visual_position, (original_index, _) in enumerate(visual_entries, start=1)
        }
        for row in extracted_visual:
            visual_file_index = int(row.get("file_index", 1) or 1)
            file_index = visual_index_map.get(visual_file_index, visual_file_index)
            row = {**row, "file_index": file_index}
            extracted_by_file.setdefault(file_index, []).append(row)

    for file_index, file_path in enumerate(upload_paths, start=1):
        if file_path.suffix.lower() != ".csv":
            continue
        extracted_by_file[file_index] = vision_service.extract(file_path, upload_context=upload_context)

    for file_index in range(1, len(upload_paths) + 1):
        extracted = extracted_by_file.get(file_index, [])
        for row in extracted:
            order += 1
            raw_line_id = str(row.get("line_id") or f"L{order}")
            lines.append(
                RawExtractedLine(
                    line_id=f"F{file_index}-{raw_line_id}",
                    raw_text=row.get("raw_text", "").strip(),
                    section_header=row.get("section_header", "").strip(),
                    section_type=row.get("section_type", "unknown"),
                    quantity_raw=row.get("quantity_raw", ""),
                    quantity=float(row.get("quantity", 1) or 1),
                    dimensions_raw=row.get("dimensions_raw", ""),
                    length=str(row.get("length", "") or ""),
                    width=str(row.get("width", "") or ""),
                    height=str(row.get("height", "") or ""),
                    unit=str(row.get("unit", "") or ""),
                    indentation_level=int(row.get("indentation_level", 0) or 0),
                    bullet_style=row.get("bullet_style", ""),
                    source_page=int(row.get("source_page", 1) or 1),
                    source_order=order,
                    confidence=float(row.get("confidence", 0) or 0),
                    unresolved_tokens=list(row.get("unresolved_tokens", []) or []),
                )
            )
    return lines, merged_context


def stage_b_interpret(
    stage_a_lines: list[RawExtractedLine],
    interpreter: ContextInterpreter,
    upload_context: str = "",
) -> list[ContextualizedLine]:
    return interpreter.interpret(stage_a_lines, upload_context=upload_context)


def stage_c_prepare_for_matching(stage_b_lines: list[ContextualizedLine]) -> list[MatchReadyLine]:
    out: list[MatchReadyLine] = []
    for line in stage_b_lines:
        attributes = {
            "brand": line.brand,
            "color": line.color,
            "product_family": line.product_family,
            "product_type": line.product_type,
            "profile": line.profile,
            "material": line.material,
            "finish": line.finish,
            "inferred_use": line.inferred_use,
        }
        match_text = line.normalized_description or line.raw_text
        parts = [line.brand, line.color, line.product_family, line.product_type, line.dimensions, match_text]
        normalized_match_text = " ".join(part for part in parts if part).strip()
        out.append(
            MatchReadyLine(
                line_id=line.line_id,
                quantity=line.quantity,
                raw_text=line.raw_text,
                normalized_description=line.normalized_description or line.raw_text,
                match_text=normalized_match_text or match_text,
                brand=line.brand,
                color=line.color,
                product_family=line.product_family,
                product_type=line.product_type,
                size=line.dimensions,
                attributes=attributes,
                ambiguity_flags=line.ambiguity_flags,
                needs_review=bool(line.ambiguity_flags),
                review_reason=line.review_reason,
                section_header=line.inherited_section_header,
            )
        )
    return out


def _save_debug_artifacts(session_id: int | None, stage_a: list[RawExtractedLine], stage_b: list[ContextualizedLine], stage_c: list[MatchReadyLine]) -> None:
    base_dir = Path(current_app.root_path).parent / "data" / "parse_debug"
    folder_name = f"session_{session_id}" if session_id else "session_unknown"
    session_dir = base_dir / folder_name
    session_dir.mkdir(parents=True, exist_ok=True)

    (session_dir / "stage_a_raw_extract.json").write_text(json.dumps([asdict(line) for line in stage_a], indent=2), encoding="utf-8")
    (session_dir / "stage_b_contextualized.json").write_text(json.dumps([asdict(line) for line in stage_b], indent=2), encoding="utf-8")
    (session_dir / "stage_c_match_ready.json").write_text(json.dumps([asdict(line) for line in stage_c], indent=2), encoding="utf-8")

    logger.info("parse_debug_artifacts_saved", extra={"session_id": session_id, "stage": "parse_debug"})
