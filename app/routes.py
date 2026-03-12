"""
Flask Routes
------------
Handles all HTTP endpoints for the material list ingestor application.
"""

import io
import json
import logging
import os
import uuid
from pathlib import Path

import pandas as pd
from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.utils import secure_filename

from app import db
from app.models import ERPItem, ExtractedItem, ProcessingSession, ItemAlias, MatchFeedbackEvent
from app.services import ocr_service, ai_parser, chatgpt_parser, item_matcher

logger = logging.getLogger(__name__)
main = Blueprint("main", __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def allowed_file(filename: str) -> bool:
    ext = Path(filename).suffix.lstrip(".").lower()
    return ext in current_app.config["ALLOWED_EXTENSIONS"]


def save_upload(file) -> Path:
    ext = Path(secure_filename(file.filename)).suffix
    unique_name = f"{uuid.uuid4().hex}{ext}"
    dest = Path(current_app.config["UPLOAD_FOLDER"]) / unique_name
    file.save(dest)
    return dest


# ---------------------------------------------------------------------------
# Index / Upload
# ---------------------------------------------------------------------------

@main.route("/")
def index():
    recent_sessions = (
        ProcessingSession.query.order_by(ProcessingSession.created_at.desc()).limit(10).all()
    )
    claude_available = bool(current_app.config.get("ANTHROPIC_API_KEY"))
    openai_available = bool(current_app.config.get("OPENAI_API_KEY"))
    default_provider = current_app.config.get("DEFAULT_AI_PROVIDER", "claude")
    return render_template(
        "index.html",
        sessions=recent_sessions,
        claude_available=claude_available,
        openai_available=openai_available,
        default_provider=default_provider,
    )


@main.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        flash("No file selected.", "error")
        return redirect(url_for("main.index"))

    file = request.files["file"]
    if not file.filename:
        flash("No file selected.", "error")
        return redirect(url_for("main.index"))

    if not allowed_file(file.filename):
        flash("Unsupported file type. Please upload JPG, PNG, or PDF.", "error")
        return redirect(url_for("main.index"))

    # Save uploaded file
    try:
        file_path = save_upload(file)
    except Exception as exc:
        logger.exception("Failed to save uploaded file")
        flash(f"Could not save the uploaded file: {exc}", "error")
        return redirect(url_for("main.index"))
    ext = file_path.suffix.lstrip(".").lower()

    session = ProcessingSession(
        filename=secure_filename(file.filename),
        file_type=ext,
        status="pending",
    )
    db.session.add(session)
    db.session.commit()

    # --- Step 1: OCR ---
    try:
        raw_text = ocr_service.extract_text(file_path)
        session.raw_ocr_text = raw_text
        session.status = "ocr_complete"
        db.session.commit()
    except Exception as exc:
        logger.exception("OCR failed for session %d", session.id)
        session.status = "error"
        session.error_message = f"OCR failed: {exc}"
        db.session.commit()
        flash(f"Could not read the file: {exc}", "error")
        return redirect(url_for("main.index"))
    finally:
        # Clean up uploaded file after reading
        try:
            os.unlink(file_path)
        except OSError:
            pass

    if not raw_text.strip():
        session.status = "error"
        session.error_message = "OCR produced no text. The image may be unreadable."
        db.session.commit()
        flash("The file appears blank or unreadable. Try a clearer image.", "error")
        return redirect(url_for("main.index"))

    # --- Step 2: AI parse ---
    provider = request.form.get("ai_provider", "").strip().lower()
    if provider not in ("claude", "openai"):
        provider = current_app.config.get("DEFAULT_AI_PROVIDER", "claude")

    if provider == "openai":
        api_key = current_app.config.get("OPENAI_API_KEY", "")
        if not api_key:
            session.status = "error"
            session.error_message = "OPENAI_API_KEY is not configured."
            db.session.commit()
            flash("ChatGPT parsing is not configured. Set OPENAI_API_KEY.", "error")
            return redirect(url_for("main.index"))
        try:
            parsed_items = chatgpt_parser.parse_material_list(
                raw_text,
                api_key=api_key,
                model=current_app.config["OPENAI_MODEL"],
            )
            session.status = "parsed"
            db.session.commit()
        except Exception as exc:
            logger.exception("ChatGPT parsing failed for session %d", session.id)
            session.status = "error"
            session.error_message = f"ChatGPT parsing failed: {exc}"
            db.session.commit()
            flash(f"ChatGPT parsing failed: {exc}", "error")
            return redirect(url_for("main.index"))
    else:
        api_key = current_app.config.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            session.status = "error"
            session.error_message = "ANTHROPIC_API_KEY is not configured."
            db.session.commit()
            flash("AI parsing is not configured. Set ANTHROPIC_API_KEY.", "error")
            return redirect(url_for("main.index"))
        try:
            parsed_items = ai_parser.parse_material_list(
                raw_text,
                api_key=api_key,
                model=current_app.config["CLAUDE_MODEL"],
            )
            session.status = "parsed"
            db.session.commit()
        except Exception as exc:
            logger.exception("Claude parsing failed for session %d", session.id)
            session.status = "error"
            session.error_message = f"AI parsing failed: {exc}"
            db.session.commit()
            flash(f"AI parsing failed: {exc}", "error")
            return redirect(url_for("main.index"))

    if not parsed_items:
        session.status = "error"
        session.error_message = "AI returned no items from the material list."
        db.session.commit()
        flash("No items could be extracted from the material list.", "warning")
        return redirect(url_for("main.index"))

    # --- Step 3: Item matching ---
    erp_items = ERPItem.query.all()
    descriptions = [item["description"] for item in parsed_items]

    if erp_items:
        try:
            match_results = item_matcher.match_items_batch(
                descriptions,
                erp_items,
                model_name=current_app.config["EMBEDDING_MODEL"],
                fuzzy_weight=current_app.config["FUZZY_WEIGHT"],
                vector_weight=current_app.config["VECTOR_WEIGHT"],
            )
        except Exception as exc:
            logger.exception("Item matching failed for session %d", session.id)
            match_results = [item_matcher._no_match() for _ in parsed_items]
    else:
        match_results = [item_matcher._no_match() for _ in parsed_items]

    for parsed, match in zip(parsed_items, match_results):
        extracted = ExtractedItem(
            session_id=session.id,
            quantity=parsed["quantity"],
            raw_description=parsed["description"],
            matched_item_code=match["matched_item_code"],
            matched_description=match["matched_description"],
            confidence_score=match["confidence_score"],
            fuzzy_score=match["fuzzy_score"],
            vector_score=match["vector_score"],
        )
        db.session.add(extracted)

    session.status = "matched"
    db.session.commit()

    return redirect(url_for("main.review", session_id=session.id))


# ---------------------------------------------------------------------------
# Review Screen
# ---------------------------------------------------------------------------

@main.route("/review/<int:session_id>")
def review(session_id):
    session = ProcessingSession.query.get_or_404(session_id)
    items = ExtractedItem.query.filter_by(session_id=session_id).all()
    threshold = current_app.config["CONFIDENCE_THRESHOLD"]
    # erp_items is intentionally NOT loaded here — the review template uses
    # the /api/erp-items JS autocomplete endpoint, so loading the entire
    # catalog into memory for every review page visit was wasted RAM.
    return render_template(
        "review.html",
        session=session,
        items=items,
        threshold=threshold,
    )


@main.route("/review/<int:session_id>/save", methods=["POST"])
def save_review(session_id):
    """Save user edits to quantities and matched items."""
    session = ProcessingSession.query.get_or_404(session_id)
    data = request.get_json()

    if not data or "items" not in data:
        return jsonify({"error": "Invalid payload"}), 400

    for item_data in data["items"]:
        item = ExtractedItem.query.get(item_data.get("id"))
        if not item or item.session_id != session_id:
            continue
        if "quantity" in item_data:
            try:
                item.final_quantity = float(item_data["quantity"])
            except (TypeError, ValueError):
                pass
        old_effective_code = item.effective_item_code()
        if "item_code" in item_data:
            item.final_item_code = item_data["item_code"] or None
        item.is_confirmed = bool(item_data.get("confirmed", False))
        item.is_skipped = bool(item_data.get("skipped", False))

        alias_key = item_matcher.normalise_description(item.raw_description)
        new_effective_code = item.effective_item_code()
        if new_effective_code and new_effective_code != old_effective_code:
            alias = ItemAlias.query.filter_by(alias=alias_key).first()
            if alias:
                alias.sku = new_effective_code
            else:
                db.session.add(ItemAlias(alias=alias_key, sku=new_effective_code))

        db.session.add(MatchFeedbackEvent(
            session_id=session_id,
            extracted_item_id=item.id,
            raw_description=item.raw_description,
            normalized_description=alias_key,
            predicted_sku=item.matched_item_code,
            final_sku=new_effective_code,
            was_corrected=bool(new_effective_code and new_effective_code != item.matched_item_code),
            was_skipped=item.is_skipped,
            confidence_score=float(item.confidence_score or 0.0),
            fuzzy_score=float(item.fuzzy_score or 0.0),
            vector_score=float(item.vector_score or 0.0),
        ))

    session.status = "reviewed"
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# ERP Item Search (for autocomplete in review)
# ---------------------------------------------------------------------------

@main.route("/api/erp-items")
def api_erp_items():
    q = request.args.get("q", "").strip()
    query = ERPItem.query
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(ERPItem.item_code.ilike(like), ERPItem.description.ilike(like))
        )
    items = query.order_by(ERPItem.description).limit(50).all()
    return jsonify([item.to_dict() for item in items])


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@main.route("/export/<int:session_id>/<fmt>")
def export(session_id, fmt):
    session = ProcessingSession.query.get_or_404(session_id)
    items = ExtractedItem.query.filter_by(session_id=session_id).all()

    # Collect all item codes in one pass, then load ERP records in a single query
    active_items = [item for item in items if not item.is_skipped]
    codes_needed = list({item.effective_item_code() for item in active_items if item.effective_item_code()})
    erp_by_code = (
        {e.item_code: e for e in ERPItem.query.filter(ERPItem.item_code.in_(codes_needed)).all()}
        if codes_needed else {}
    )

    rows = []
    for item in active_items:
        code = item.effective_item_code()
        erp = erp_by_code.get(code) if code else None
        rows.append({
            "quantity": item.effective_quantity(),
            "item_code": code or "",
            "description": erp.description if erp else item.raw_description,
        })

    if fmt == "json":
        return Response(
            json.dumps(rows, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="order_{session_id}.json"'},
        )

    df = pd.DataFrame(rows, columns=["quantity", "item_code", "description"])

    if fmt == "csv":
        csv_data = df.to_csv(index=False)
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="order_{session_id}.csv"'},
        )

    if fmt == "xlsx":
        buf = io.BytesIO()
        df.to_excel(buf, index=False)
        buf.seek(0)
        return Response(
            buf.read(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="order_{session_id}.xlsx"'},
        )

    return jsonify({"error": f"Unknown format: {fmt}"}), 400


# ---------------------------------------------------------------------------
# Catalog Management
# ---------------------------------------------------------------------------

@main.route("/catalog")
def catalog():
    page = request.args.get("page", 1, type=int)
    q = request.args.get("q", "").strip()
    query = ERPItem.query
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(ERPItem.item_code.ilike(like), ERPItem.description.ilike(like))
        )
    pagination = query.order_by(ERPItem.item_code).paginate(page=page, per_page=50)
    return render_template("catalog.html", pagination=pagination, q=q, item_count=ERPItem.query.count())


@main.route("/catalog/upload", methods=["POST"])
def catalog_upload():
    """Upload a CSV file to populate the ERP item catalog."""
    if "file" not in request.files:
        flash("No file provided.", "error")
        return redirect(url_for("main.catalog"))

    file = request.files["file"]
    if not file.filename or not file.filename.lower().endswith(".csv"):
        flash("Please upload a CSV file.", "error")
        return redirect(url_for("main.catalog"))

    try:
        df = pd.read_csv(file)
    except Exception as exc:
        flash(f"Could not parse CSV: {exc}", "error")
        return redirect(url_for("main.catalog"))

    # Normalise column names to lowercase
    df.columns = df.columns.str.lower()

    # Accept "sku" as an alias for "item_code"
    if "sku" in df.columns and "item_code" not in df.columns:
        df = df.rename(columns={"sku": "item_code"})

    required_cols = {"item_code", "description"}
    missing = required_cols - set(df.columns)
    if missing:
        flash(f"CSV is missing required columns: {', '.join(missing)}", "error")
        return redirect(url_for("main.catalog"))

    replace_all = request.form.get("replace_all") == "1"
    if replace_all:
        ERPItem.query.delete()
        db.session.commit()

    # Build a list of valid rows first
    valid_rows = []
    for _, row in df.iterrows():
        code = str(row["item_code"]).strip()
        desc = str(row["description"]).strip()
        if code and desc:
            valid_rows.append((code, row))

    # Load all existing items in one query instead of N per-row SELECTs
    all_codes = [code for code, _ in valid_rows]
    existing_map = {}
    if all_codes:
        existing_map = {
            item.item_code: item
            for item in ERPItem.query.filter(ERPItem.item_code.in_(all_codes)).all()
        }

    added = 0
    updated = 0
    _CHUNK = 500  # flush to DB in chunks to keep transaction memory bounded
    for i, (code, row) in enumerate(valid_rows):
        desc = str(row["description"]).strip()
        existing = existing_map.get(code)
        if existing:
            existing.description = desc
            existing.keywords = str(row.get("keywords", "")).strip()
            existing.category = str(row.get("category", "")).strip()
            existing.material_category = str(row.get("material_category", "")).strip()
            existing.size = str(row.get("size", "")).strip()
            existing.length = str(row.get("length", "")).strip()
            existing.brand = str(row.get("brand", "")).strip()
            existing.normalized_name = str(row.get("normalized_name", "")).strip()
            existing.unit_of_measure = str(row.get("unit_of_measure", "EA")).strip()
            existing.embedding = None  # invalidate stale embedding
            updated += 1
        else:
            item = ERPItem(
                item_code=code,
                description=desc,
                keywords=str(row.get("keywords", "")).strip(),
                category=str(row.get("category", "")).strip(),
                material_category=str(row.get("material_category", "")).strip(),
                size=str(row.get("size", "")).strip(),
                length=str(row.get("length", "")).strip(),
                brand=str(row.get("brand", "")).strip(),
                normalized_name=str(row.get("normalized_name", "")).strip(),
                unit_of_measure=str(row.get("unit_of_measure", "EA")).strip(),
            )
            db.session.add(item)
            added += 1

        # Flush in chunks so the session doesn't accumulate unbounded objects
        if (i + 1) % _CHUNK == 0:
            db.session.flush()

    db.session.commit()

    # Rebuild vector index for the current catalog
    all_items = ERPItem.query.all()
    try:
        item_matcher.build_index(all_items, current_app.config["EMBEDDING_MODEL"])
        embed_msg = f" Vector index built for {len(all_items)} items."
    except Exception as exc:
        logger.warning("Vector index build failed: %s", exc)
        embed_msg = " (Vector index will be built on first match.)"

    flash(
        f"Catalog updated: {added} items added, {updated} items updated.{embed_msg}",
        "success",
    )
    return redirect(url_for("main.catalog"))


@main.route("/catalog/delete", methods=["POST"])
def catalog_delete():
    ERPItem.query.delete()
    db.session.commit()
    flash("Catalog cleared.", "warning")
    return redirect(url_for("main.catalog"))


# ---------------------------------------------------------------------------
# Session raw text (debug/review)
# ---------------------------------------------------------------------------

@main.route("/session/<int:session_id>/raw")
def session_raw(session_id):
    session = ProcessingSession.query.get_or_404(session_id)
    return Response(session.raw_ocr_text or "", mimetype="text/plain")


# ---------------------------------------------------------------------------
# Health check (used by Render / Fly.io / Docker)
# ---------------------------------------------------------------------------

@main.route("/health")
def health():
    """Lightweight liveness + readiness probe."""
    try:
        # Verify DB is reachable
        db.session.execute(db.text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    # Use a fast existence check rather than COUNT(*) over the full table —
    # this endpoint is polled every 15 s and must complete well within the
    # 5 s health-check timeout even under concurrent load.
    has_catalog = False
    if db_ok:
        try:
            has_catalog = ERPItem.query.with_entities(ERPItem.id).limit(1).first() is not None
        except Exception:
            pass

    status = "ok" if db_ok else "degraded"
    return jsonify({
        "status": status,
        "db": db_ok,
        "catalog_loaded": has_catalog,
        "anthropic_key_set": bool(current_app.config.get("ANTHROPIC_API_KEY")),
        "openai_key_set": bool(current_app.config.get("OPENAI_API_KEY")),
        "default_ai_provider": current_app.config.get("DEFAULT_AI_PROVIDER", "claude"),
    }), 200 if db_ok else 503
