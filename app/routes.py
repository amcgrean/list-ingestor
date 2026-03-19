"""
Flask Routes
------------
Handles all HTTP endpoints for the material list ingestor application.
"""

import io
import json
import logging
import os
import tempfile
import time
import uuid
from typing import Any
from pathlib import Path

import pandas as pd
from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from app import db
from app.models import (
    Branch,
    BranchCatalogItem,
    ERPItem,
    ExtractedItem,
    IngesterMetrics,
    ItemAlias,
    MatchFeedbackEvent,
    ProcessingSession,
    SessionFeedbackEvent,
    User,
)
from app.services.catalog_importer import (
    clean_csv_value as _clean_csv_value,
    export_catalog_artifacts as _export_catalog_artifacts,
    import_catalog_dataframe,
    prepare_catalog_dataframe,
    prune_orphan_erp_items as _prune_orphan_erp_items,
)
from app.services import item_matcher
from app.services import metrics_service
from app.services.customer_job_context import match_customer_job_context
from app.services.upload_context import (
    context_to_json,
    enrich_description_for_matching,
    merge_document_contexts,
    normalize_document_context,
)
from app.services.parse_pipeline import parse_uploads
from app.services.sku_pipeline import CatalogValidationError
import sys, os as _os
# Add project root so services/ package is importable from within the Flask app
_PROJECT_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
from services.openai_vision import extract_document_data_from_images as _vision_extract_documents

logger = logging.getLogger(__name__)
main = Blueprint("main", __name__)
CF_ACCESS_EMAIL_HEADER = "Cf-Access-Authenticated-User-Email"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def allowed_file(filename: str) -> bool:
    ext = Path(filename).suffix.lstrip(".").lower()
    return ext in current_app.config["ALLOWED_EXTENSIONS"]


def parse_csv_items(file_path: Path) -> list[dict[str, Any]]:
    """Extract quantity/description rows from a CSV upload."""
    df = pd.read_csv(file_path)
    lower_cols = {c.lower(): c for c in df.columns}

    description_col = next((lower_cols[c] for c in ("description", "item", "material", "name") if c in lower_cols), None)
    if not description_col:
        raise ValueError("CSV must contain a description-like column (description/item/material/name).")

    quantity_col = next((lower_cols[c] for c in ("quantity", "qty", "count") if c in lower_cols), None)

    parsed: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        desc = str(row.get(description_col, "")).strip()
        if not desc or desc.lower() == "nan":
            continue
        qty = 1.0
        if quantity_col:
            try:
                qty = float(row.get(quantity_col, 1) or 1)
            except (TypeError, ValueError):
                qty = 1.0
        parsed.append({"quantity": qty, "description": desc})
    return parsed


def save_upload(file) -> Path:
    ext = Path(secure_filename(file.filename)).suffix
    fd, tmp_path = tempfile.mkstemp(suffix=ext)
    try:
        os.close(fd)
        file.save(tmp_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return Path(tmp_path)

def _resolve_system_id() -> str:
    return (
        request.args.get("system_id")
        or request.form.get("system_id")
        or request.headers.get("X-System-Id")
        or ""
    ).strip()


def _read_catalog_upload(file) -> pd.DataFrame:
    filename = (file.filename or "").lower()
    if filename.endswith((".xlsx", ".xls")):
        return pd.read_excel(file)
    return pd.read_csv(file)


def _load_json_blob(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _all_branches():
    return Branch.query.filter_by(is_active=True).order_by(Branch.code).all()


def _cloudflare_email() -> str:
    header_name = current_app.config.get(
        "CLOUDFLARE_ACCESS_EMAIL_HEADER", CF_ACCESS_EMAIL_HEADER
    )
    return (request.headers.get(header_name) or "").strip().lower()


def _refresh_current_user() -> User | None:
    email = _cloudflare_email()
    user = None
    if email:
        user = User.query.filter_by(email=email).first()
        if user and user.is_active:
            session["user_id"] = user.id
    elif session.get("user_id"):
        user = db.session.get(User, session["user_id"])
        if user and not user.is_active:
            session.pop("user_id", None)
            user = None

    if user:
        user.last_seen_at = db.func.now()
        db.session.commit()
    g.current_user = user
    return user


def _current_user() -> User | None:
    if hasattr(g, "current_user"):
        return g.current_user
    return _refresh_current_user()


def _get_branch_for_request(allow_session_override: bool = True) -> Branch | None:
    branch_id = request.values.get("branch_id", type=int)
    if branch_id:
        branch = Branch.query.filter_by(id=branch_id, is_active=True).first()
        if branch:
            if allow_session_override:
                session["branch_id"] = branch.id
            g.current_branch = branch
            return branch

    if allow_session_override and session.get("branch_id"):
        branch = Branch.query.filter_by(id=session["branch_id"], is_active=True).first()
        if branch:
            g.current_branch = branch
            return branch

    user = _current_user()
    if user and user.default_branch and user.default_branch.is_active:
        if allow_session_override:
            session["branch_id"] = user.default_branch.id
        g.current_branch = user.default_branch
        return user.default_branch

    branch = Branch.query.filter_by(is_active=True).order_by(Branch.code).first()
    if branch and allow_session_override:
        session["branch_id"] = branch.id
    g.current_branch = branch
    return branch


def _current_branch() -> Branch | None:
    if hasattr(g, "current_branch"):
        return g.current_branch
    return _get_branch_for_request()


def _require_user() -> User:
    user = _current_user()
    if not user:
        if current_app.config.get("ALLOW_LOCAL_LOGIN", True):
            flash("Please sign in with your email to continue.", "warning")
            return redirect(url_for("main.login"))
        abort(403)
    return user


def _require_admin() -> User:
    user = _current_user()
    if not user:
        if current_app.config.get("ALLOW_LOCAL_LOGIN", True):
            flash("Please sign in to access admin controls.", "warning")
            return redirect(url_for("main.login"))
        abort(403)
    if not user.is_admin:
        abort(403)
    return user


def _branch_items_query(branch: Branch | None):
    query = ERPItem.query.join(BranchCatalogItem, BranchCatalogItem.erp_item_id == ERPItem.id)
    if branch:
        query = query.filter(BranchCatalogItem.branch_id == branch.id)
    return query


def _branch_items(branch: Branch | None) -> list[ERPItem]:
    if branch is None:
        return []
    return _branch_items_query(branch).order_by(ERPItem.item_code).all()


@main.before_app_request
def load_request_context():
    _refresh_current_user()
    _get_branch_for_request()


# ---------------------------------------------------------------------------
# Index / Upload
# ---------------------------------------------------------------------------

@main.route("/login", methods=["GET", "POST"])
def login():
    if _current_user():
        return redirect(url_for("main.index"))

    if not current_app.config.get("ALLOW_LOCAL_LOGIN", True):
        abort(403)

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        user = User.query.filter_by(email=email, is_active=True).first()
        if user:
            session["user_id"] = user.id
            if user.default_branch_id:
                session["branch_id"] = user.default_branch_id
            flash(f"Signed in as {user.email}.", "success")
            return redirect(url_for("main.index"))
        flash("That email is not authorized for this app yet.", "error")

    return render_template(
        "login.html",
        users=User.query.filter_by(is_active=True).order_by(User.email).all(),
    )


@main.route("/logout", methods=["POST"])
def logout():
    session.pop("user_id", None)
    flash("Signed out.", "success")
    return redirect(url_for("main.login"))


@main.route("/admin/users", methods=["GET", "POST"])
def admin_users():
    user = _current_user()
    if not user:
        if current_app.config.get("ALLOW_LOCAL_LOGIN", True):
            flash("Please sign in to access admin controls.", "warning")
            return redirect(url_for("main.login"))
        abort(403)
    if not user.is_admin:
        abort(403)

    if request.method == "POST":
        action = request.form.get("action", "create")
        if action == "create":
            email = (request.form.get("email") or "").strip().lower()
            default_branch = db.session.get(Branch, request.form.get("default_branch_id", type=int))
            if not email:
                flash("Email is required.", "error")
            elif User.query.filter_by(email=email).first():
                flash("That user already exists.", "warning")
            else:
                db.session.add(
                    User(
                        email=email,
                        full_name=(request.form.get("full_name") or "").strip(),
                        is_active=True,
                        is_admin=False,
                        default_branch=default_branch,
                    )
                )
                db.session.commit()
                flash(f"Created user {email}.", "success")
        elif action == "update":
            target = User.query.get_or_404(request.form.get("user_id", type=int))
            target.full_name = (request.form.get("full_name") or "").strip()
            target.is_active = request.form.get("is_active") == "1"
            target.default_branch = db.session.get(Branch, request.form.get("default_branch_id", type=int))
            if target.email == current_app.config.get("BOOTSTRAP_ADMIN_EMAIL"):
                target.is_admin = True
                for other in User.query.filter(User.id != target.id, User.is_admin.is_(True)).all():
                    other.is_admin = False
            else:
                target.is_admin = False
            db.session.commit()
            flash(f"Updated {target.email}.", "success")
        return redirect(url_for("main.admin_users"))

    return render_template(
        "admin_users.html",
        users=User.query.order_by(User.email).all(),
        branches=_all_branches(),
    )

@main.route("/")
def index():
    user = _current_user()
    if not user:
        if current_app.config.get("ALLOW_LOCAL_LOGIN", True):
            return redirect(url_for("main.login"))
        abort(403)
    recent_sessions = (
        ProcessingSession.query.order_by(ProcessingSession.created_at.desc()).limit(10).all()
    )
    claude_available = bool(current_app.config.get("ANTHROPIC_API_KEY"))
    openai_available = bool(current_app.config.get("OPENAI_API_KEY"))
    default_provider = current_app.config.get("DEFAULT_AI_PROVIDER", "claude")
    return render_template(
        "index.html",
        sessions=recent_sessions,
        branches=_all_branches(),
        selected_branch=_current_branch(),
        claude_available=claude_available,
        openai_available=openai_available,
        default_provider=default_provider,
    )


@main.route("/upload", methods=["POST"])
def upload():
    user = _current_user()
    if not user:
        if current_app.config.get("ALLOW_LOCAL_LOGIN", True):
            flash("Please sign in to upload a list.", "warning")
            return redirect(url_for("main.login"))
        abort(403)

    branch = _get_branch_for_request()
    if not branch:
        flash("Select a branch before uploading.", "error")
        return redirect(url_for("main.index"))

    files = request.files.getlist("files")
    if not files:
        legacy = request.files.get("file")
        files = [legacy] if legacy else []

    files = [f for f in files if f and f.filename]
    if not files:
        flash("No file selected.", "error")
        return redirect(url_for("main.index"))

    invalid = [f.filename for f in files if not allowed_file(f.filename)]
    if invalid:
        flash("Unsupported file type. Only images, PDFs, and CSV files are allowed.", "error")
        return redirect(url_for("main.index"))

    saved_uploads: list[tuple[str, Path]] = []
    for file in files:
        try:
            saved_uploads.append((secure_filename(file.filename), save_upload(file)))
        except Exception as exc:
            logger.exception("Failed to save uploaded file")
            flash(f"Could not save an uploaded file: {exc}", "error")
            for _, path in saved_uploads:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            return redirect(url_for("main.index"))

    system_id = _resolve_system_id()
    upload_context = (request.form.get("upload_context") or "").strip()
    first_ext = saved_uploads[0][1].suffix.lstrip(".").lower()
    session = ProcessingSession(
        filename=", ".join(name for name, _ in saved_uploads)[:255],
        file_type=first_ext if len(saved_uploads) == 1 else "batch",
        branch=branch,
        user=user,
        status="pending",
        system_id=system_id or branch.code,
        upload_context=upload_context or None,
    )
    db.session.add(session)
    db.session.commit()

    t_start = time.perf_counter()
    ai_ms = match_ms = None
    ai_parse_error = match_error = False
    provider = "openai"

    # --- Step 1: parse each upload ---
    api_key = current_app.config.get("OPENAI_API_KEY", "")
    t0 = time.perf_counter()
    parsed_items: list[dict[str, Any]] = []
    document_contexts: list[dict[str, Any]] = []
    parse_stage_label = "legacy"
    try:
        upload_paths = [path for _, path in saved_uploads]
        use_context_pipeline = current_app.config.get("ENABLE_CONTEXT_PIPELINE", True)

        if use_context_pipeline:
            try:
                _, _, stage_c_lines, stage_document_context = parse_uploads(
                    upload_paths,
                    api_key=api_key,
                    session_id=session.id,
                    upload_context=upload_context,
                )
                parsed_items = [
                    {
                        "line_id": line.line_id,
                        "quantity": line.quantity,
                        "description": line.raw_text,
                        "raw_text": line.raw_text,
                        "source_description": line.raw_text,
                        "applied_context": [value for value in (line.section_header,) if value],
                        "normalized_description": line.normalized_description,
                        "section_header": line.section_header,
                        "brand": line.brand,
                        "color": line.color,
                        "product_family": line.product_family,
                        "product_type": line.product_type,
                        "ambiguity_flags": line.ambiguity_flags,
                        "review_reason": line.review_reason,
                        "needs_review": line.needs_review,
                        "match_text": line.match_text,
                    }
                    for line in stage_c_lines
                ]
                parse_stage_label = "context_stage_c"
                if stage_document_context:
                    document_contexts.append(stage_document_context)
            except Exception:
                if not current_app.config.get("CONTEXT_PIPELINE_FALLBACK_TO_LEGACY", True):
                    raise
                logger.exception(
                    "Context pipeline failed; falling back to legacy single-pass parsing"
                )

        if not parsed_items:
            csv_uploads = []
            visual_uploads = []
            for _, file_path in saved_uploads:
                ext = file_path.suffix.lstrip(".").lower()
                if ext == "csv":
                    csv_uploads.append(file_path)
                else:
                    visual_uploads.append(file_path)

            for file_path in csv_uploads:
                parsed_items.extend(parse_csv_items(file_path))

            if visual_uploads:
                if not api_key:
                    raise RuntimeError("OPENAI_API_KEY is not configured for image/pdf parsing.")

                vision_payload = _vision_extract_documents(
                    visual_uploads,
                    api_key=api_key,
                    model=current_app.config["OPENAI_MODEL"],
                    upload_context=upload_context,
                )
                parsed_items.extend(vision_payload["items"])
                document_contexts.append(vision_payload["document_context"])
        elif api_key and not document_contexts:
            visual_uploads = [
                file_path
                for _, file_path in saved_uploads
                if file_path.suffix.lstrip(".").lower() != "csv"
            ]
            if visual_uploads:
                try:
                    vision_payload = _vision_extract_documents(
                        visual_uploads,
                        api_key=api_key,
                        model=current_app.config["OPENAI_MODEL"],
                        upload_context=upload_context,
                    )
                    document_contexts.append(vision_payload["document_context"])
                except Exception:
                    logger.warning(
                        "Document context extraction failed for uploaded batch",
                        exc_info=True,
                    )

        ai_ms = int((time.perf_counter() - t0) * 1000)
        merged_context = merge_document_contexts(document_contexts)
        synced_context_match = match_customer_job_context(
            customer_name=merged_context.get("customer_name", ""),
            project_name=merged_context.get("project_name", ""),
            upload_context=upload_context,
            branch_code=branch.code if branch else "",
        )
        synced_context_payload = (
            synced_context_match.to_session_payload() if synced_context_match else {}
        )
        session.extracted_context_json = context_to_json(merged_context)
        session.matched_context_json = (
            json.dumps(synced_context_payload, sort_keys=True) if synced_context_payload else None
        )
        raw_text_lines = []
        if upload_context:
            raw_text_lines.append(f"Upload context: {upload_context}")
        if merged_context.get("summary"):
            raw_text_lines.append(f"Document summary: {merged_context['summary']}")
        if merged_context.get("customer_name"):
            raw_text_lines.append(f"Customer: {merged_context['customer_name']}")
        if merged_context.get("project_name"):
            raw_text_lines.append(f"Project: {merged_context['project_name']}")
        if merged_context.get("global_material_context"):
            raw_text_lines.append(
                "Global material context: " + ", ".join(merged_context["global_material_context"])
            )
        if merged_context.get("job_notes"):
            raw_text_lines.append("Job notes: " + " | ".join(merged_context["job_notes"]))
        if synced_context_payload:
            raw_text_lines.append(
                "Matched cloud context: "
                + " / ".join(
                    part for part in (
                        synced_context_payload.get("customer_name"),
                        synced_context_payload.get("project_name"),
                    ) if part
                )
            )
            if synced_context_payload.get("material_context"):
                raw_text_lines.append(
                    f"Cloud material context: {synced_context_payload['material_context']}"
                )
            if synced_context_payload.get("job_notes"):
                raw_text_lines.append(f"Cloud job notes: {synced_context_payload['job_notes']}")
        raw_text_lines.extend(
            f"{item.get('quantity', 1)} "
            f"{item.get('source_description') or item.get('raw_text') or item.get('description', '')}"
            for item in parsed_items
        )
        session.raw_ocr_text = "\n".join(raw_text_lines)
        session.status = "parsed"
        db.session.commit()
        logger.info("vision_parse_complete", extra={
            "session_id": session.id, "stage": "vision_parse",
            "duration_ms": ai_ms, "items": len(parsed_items),
        })
    except Exception as exc:
        ai_ms = int((time.perf_counter() - t0) * 1000)
        ai_parse_error = True
        logger.exception("Parsing failed for session %d", session.id, extra={
            "session_id": session.id, "stage": "vision_parse", "duration_ms": ai_ms,
        })
        session.status = "error"
        session.error_message = f"Parsing failed: {exc}"
        db.session.commit()
        metrics_service.save_session_metrics(
            session_id=session.id, ai_provider=provider,
            ocr_ms=None, ai_parse_ms=ai_ms, match_ms=None,
            total_ms=int((time.perf_counter() - t_start) * 1000),
            items_extracted=0, items_matched=0, items_below_threshold=0,
            avg_confidence=None, avg_fuzzy_score=None, avg_vector_score=None,
            ai_parse_error=True,
        )
        flash(f"Could not parse the upload(s): {exc}", "error")
        return redirect(url_for("main.index"))
    finally:
        for _, file_path in saved_uploads:
            try:
                os.unlink(file_path)
            except OSError:
                pass

    if not parsed_items:
        session.status = "error"
        session.error_message = "Parser returned no items from the uploaded material lists."
        db.session.commit()
        metrics_service.save_session_metrics(
            session_id=session.id, ai_provider=provider,
            ocr_ms=None, ai_parse_ms=ai_ms, match_ms=None,
            total_ms=int((time.perf_counter() - t_start) * 1000),
            items_extracted=0, items_matched=0, items_below_threshold=0,
            avg_confidence=None, avg_fuzzy_score=None, avg_vector_score=None,
            ai_parse_error=True,
        )
        flash("No items could be extracted from the uploaded material list(s).", "warning")
        return redirect(url_for("main.index"))

    # --- Step 3: Item matching ---
    erp_items = _branch_items(branch)
    if not erp_items and session.system_id:
        erp_items = item_matcher.get_catalog_for_system(
            session.system_id,
            fallback_to_global=current_app.config.get("BRANCH_MATCH_FALLBACK_GLOBAL", True),
        )
    merged_context = merge_document_contexts(document_contexts)
    synced_context_payload = _load_json_blob(session.matched_context_json)
    descriptions = [
        enrich_description_for_matching(
            item.get("match_text") or item.get("normalized_description") or item["description"],
            upload_context=upload_context,
            document_context={
                **merged_context,
                "global_material_context": list(
                    dict.fromkeys(
                        merged_context.get("global_material_context", [])
                        + ([synced_context_payload.get("material_context")] if synced_context_payload.get("material_context") else [])
                        + item.get("applied_context", [])
                    )
                ),
                "job_notes": list(
                    dict.fromkeys(
                        merged_context.get("job_notes", [])
                        + ([synced_context_payload.get("job_notes")] if synced_context_payload.get("job_notes") else [])
                    )
                ),
            },
        )
        for item in parsed_items
    ]
    threshold = current_app.config["CONFIDENCE_THRESHOLD"]

    t0 = time.perf_counter()
    if erp_items:
        try:
            match_results = item_matcher.match_items_batch(
                descriptions,
                erp_items,
                model_name=current_app.config["EMBEDDING_MODEL"],
                fuzzy_weight=current_app.config["FUZZY_WEIGHT"],
                vector_weight=current_app.config["VECTOR_WEIGHT"],
                cache_key=f"branch:{branch.id}",
            )
        except Exception:
            logger.exception("Item matching failed for session %d", session.id, extra={
                "session_id": session.id, "stage": "match",
            })
            match_error = True
            match_results = [item_matcher._no_match() for _ in parsed_items]
    else:
        match_results = [item_matcher._no_match() for _ in parsed_items]
    match_ms = int((time.perf_counter() - t0) * 1000)
    total_ms = int((time.perf_counter() - t_start) * 1000)

    logger.info("match_complete", extra={
        "session_id": session.id, "stage": "match",
        "duration_ms": match_ms, "items": len(match_results),
    })

    confidence_scores = [r["confidence_score"] for r in match_results]
    fuzzy_scores = [r["fuzzy_score"] for r in match_results]
    vector_scores = [r["vector_score"] for r in match_results]
    items_matched = sum(1 for r in match_results if r["matched_item_code"])
    items_below = sum(
        1
        for parsed, result in zip(parsed_items, match_results)
        if result["confidence_score"] < threshold or parsed.get("needs_review")
    )
    avg_conf = sum(confidence_scores) / len(confidence_scores) if confidence_scores else None
    avg_fuzzy = sum(fuzzy_scores) / len(fuzzy_scores) if fuzzy_scores else None
    avg_vec = sum(vector_scores) / len(vector_scores) if vector_scores else None

    for parsed, match in zip(parsed_items, match_results):
        extracted = ExtractedItem(
            session_id=session.id,
            quantity=parsed["quantity"],
            raw_description=parsed.get("source_description")
            or parsed.get("description")
            or parsed.get("raw_text")
            or "",
            parse_stage=parse_stage_label,
            parse_line_id=parsed.get("line_id"),
            normalized_description=parsed.get("normalized_description"),
            section_header=parsed.get("section_header"),
            brand=parsed.get("brand"),
            color=parsed.get("color"),
            product_family=parsed.get("product_family"),
            product_type=parsed.get("product_type"),
            ambiguity_flags=json.dumps(parsed.get("ambiguity_flags", [])),
            review_reason=parsed.get("review_reason"),
            matched_item_code=match["matched_item_code"],
            matched_description=match["matched_description"],
            confidence_score=match["confidence_score"],
            fuzzy_score=match["fuzzy_score"],
            vector_score=match["vector_score"],
            candidates_json=json.dumps(match.get("candidates", [])),
        )
        db.session.add(extracted)

    session.status = "matched"
    db.session.commit()

    metrics_service.save_session_metrics(
        session_id=session.id,
        ai_provider=provider,
        ocr_ms=None,
        ai_parse_ms=ai_ms,
        match_ms=match_ms,
        total_ms=total_ms,
        items_extracted=len(parsed_items),
        items_matched=items_matched,
        items_below_threshold=items_below,
        avg_confidence=avg_conf,
        avg_fuzzy_score=avg_fuzzy,
        avg_vector_score=avg_vec,
        match_error=match_error,
    )

    logger.info("upload_complete", extra={
        "session_id": session.id, "stage": "upload",
        "duration_ms": total_ms, "ai_provider": provider,
        "items": len(parsed_items),
    })

    return redirect(url_for("main.review", session_id=session.id))


# ---------------------------------------------------------------------------
# Review Screen
# ---------------------------------------------------------------------------

@main.route("/review/<int:session_id>")
def review(session_id):
    if not _current_user():
        if current_app.config.get("ALLOW_LOCAL_LOGIN", True):
            flash("Please sign in to review a session.", "warning")
            return redirect(url_for("main.login"))
        abort(403)
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
        selected_branch=session.branch,
        extracted_context=normalize_document_context(_load_json_blob(session.extracted_context_json)),
        matched_context=_load_json_blob(session.matched_context_json),
    )


@main.route("/review/<int:session_id>/save", methods=["POST"])
def save_review(session_id):
    """Save user edits to quantities and matched items."""
    if not _current_user():
        return jsonify({"error": "Unauthorized"}), 403
    session = ProcessingSession.query.get_or_404(session_id)
    data = request.get_json()

    if not data or "items" not in data:
        return jsonify({"error": "Invalid payload"}), 400

    session_comment = str(data.get("session_comment", "")).strip()
    request_reprocess = bool(data.get("request_reprocess", False))

    for item_data in data["items"]:
        item = db.session.get(ExtractedItem, item_data.get("id"))
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
                alias.usage_count = (alias.usage_count or 0) + 1
            else:
                db.session.add(ItemAlias(alias=alias_key, sku=new_effective_code, usage_count=1))

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
            feedback_comment=(item_data.get("comment") or None),
        ))

    if session_comment:
        session.session_comment = session_comment
        session.feedback_reprocess_requested = request_reprocess
        # Only create a SessionFeedbackEvent if one with this comment doesn't already exist
        # (avoids duplicates when feedback_workflow was already called before saving)
        existing_event = SessionFeedbackEvent.query.filter_by(
            session_id=session_id, comment=session_comment
        ).first()
        if not existing_event:
            db.session.add(SessionFeedbackEvent(
                session_id=session_id,
                comment=session_comment,
                requested_reprocess=request_reprocess,
            ))

    session.status = "reviewed"
    db.session.commit()

    response: dict[str, Any] = {"ok": True}
    if request_reprocess:
        response["reprocess_url"] = url_for("main.reprocess_session", session_id=session_id)
    return jsonify(response)


@main.route("/review/<int:session_id>/feedback-workflow", methods=["POST"])
def feedback_workflow(session_id):
    """Build a suggested reprocessing prompt from user feedback and log the event."""
    if not _current_user():
        return jsonify({"error": "Unauthorized"}), 403
    session = ProcessingSession.query.get_or_404(session_id)
    data = request.get_json() or {}
    comment = str(data.get("comment", "")).strip()
    if not comment:
        return jsonify({"error": "Feedback comment is required."}), 400

    items = ExtractedItem.query.filter_by(session_id=session_id).all()
    low_conf = [i for i in items if (i.confidence_score or 0) < current_app.config["CONFIDENCE_THRESHOLD"]]
    corrected = [i for i in items if i.final_item_code and i.final_item_code != i.matched_item_code]

    context_blob = {
        "session_id": session.id,
        "filename": session.filename,
        "user_feedback": comment,
        "low_confidence_items": [
            {"description": i.raw_description, "predicted_sku": i.matched_item_code, "confidence": i.confidence_score}
            for i in low_conf
        ],
        "corrected_items": [
            {"description": i.raw_description, "predicted_sku": i.matched_item_code, "final_sku": i.final_item_code}
            for i in corrected
        ],
    }

    existing_event = SessionFeedbackEvent.query.filter_by(
        session_id=session_id,
        comment=comment,
    ).first()
    if not existing_event:
        db.session.add(SessionFeedbackEvent(
            session_id=session_id,
            comment=comment,
            requested_reprocess=True,
        ))
    session.session_comment = comment
    session.feedback_reprocess_requested = True
    db.session.commit()

    suggestion = (
        "Use this feedback context to re-interpret the uploaded material list and prioritize corrected patterns. "
        f"Context JSON: {json.dumps(context_blob)}"
    )
    return jsonify({"ok": True, "suggested_prompt": suggestion, "context": context_blob})


# ---------------------------------------------------------------------------
# Reprocess Session
# ---------------------------------------------------------------------------

@main.route("/review/<int:session_id>/reprocess", methods=["POST"])
def reprocess_session(session_id):
    """Re-run item matching against the ERP catalog using updated feedback/aliases."""
    user = _current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 403

    session = ProcessingSession.query.get_or_404(session_id)
    items = ExtractedItem.query.filter_by(session_id=session_id).all()
    if not items:
        return jsonify({"error": "No items to reprocess."}), 400

    branch = session.branch
    erp_items = _branch_items(branch) if branch else []
    if not erp_items and session.system_id:
        erp_items = item_matcher.get_catalog_for_system(
            session.system_id,
            fallback_to_global=current_app.config.get("BRANCH_MATCH_FALLBACK_GLOBAL", True),
        )
    if not erp_items:
        return jsonify({"error": "No ERP catalog available for this branch."}), 400

    # Combine original upload context with any session feedback comment as extra context
    upload_ctx = session.upload_context or ""
    if session.session_comment:
        upload_ctx = f"{upload_ctx} {session.session_comment}".strip()

    merged_context = normalize_document_context(_load_json_blob(session.extracted_context_json))
    synced_context_payload = _load_json_blob(session.matched_context_json) or {}

    descriptions = [
        enrich_description_for_matching(
            item.raw_description,
            upload_context=upload_ctx,
            document_context={
                **merged_context,
                "global_material_context": list(
                    dict.fromkeys(
                        merged_context.get("global_material_context", [])
                        + ([synced_context_payload.get("material_context")] if synced_context_payload.get("material_context") else [])
                    )
                ),
                "job_notes": list(
                    dict.fromkeys(
                        merged_context.get("job_notes", [])
                        + ([synced_context_payload.get("job_notes")] if synced_context_payload.get("job_notes") else [])
                    )
                ),
            },
        )
        for item in items
    ]

    try:
        match_results = item_matcher.match_items_batch(
            descriptions,
            erp_items,
            model_name=current_app.config["EMBEDDING_MODEL"],
            fuzzy_weight=current_app.config["FUZZY_WEIGHT"],
            vector_weight=current_app.config["VECTOR_WEIGHT"],
            cache_key=f"branch:{branch.id}" if branch else "default",
        )
    except Exception:
        logger.exception("Reprocess matching failed for session %d", session_id)
        return jsonify({"error": "Matching failed during reprocess."}), 500

    for item, match in zip(items, match_results):
        # Skip items the user already confirmed — don't clobber their work
        if item.is_confirmed and item.final_item_code:
            continue
        item.matched_item_code = match["matched_item_code"]
        item.matched_description = match["matched_description"]
        item.confidence_score = match["confidence_score"]
        item.fuzzy_score = match["fuzzy_score"]
        item.vector_score = match["vector_score"]
        item.candidates_json = json.dumps(match.get("candidates", []))

    session.status = "matched"
    db.session.commit()

    return jsonify({"ok": True, "redirect": url_for("main.review", session_id=session_id)})


# ---------------------------------------------------------------------------
# ERP Item Search (for autocomplete in review)
# ---------------------------------------------------------------------------

@main.route("/api/erp-items")
def api_erp_items():
    if not _current_user():
        return jsonify({"error": "Unauthorized"}), 403
    q = request.args.get("q", "").strip()
    session_id = request.args.get("session_id", type=int)
    branch = None
    if session_id:
        session_obj = db.session.get(ProcessingSession, session_id)
        branch = session_obj.branch if session_obj else None
    if branch is None:
        branch = _current_branch()

    query = _branch_items_query(branch)
    if q:
        like = f"%{q}%"
        starts_with = f"{q}%"
        query = query.filter(
            db.or_(ERPItem.item_code.ilike(like), ERPItem.description.ilike(like))
        )
        # Order by relevance: exact item_code match first, then description starts-with, then rest
        query = query.order_by(
            db.case(
                (ERPItem.item_code.ilike(q), 0),
                (ERPItem.description.ilike(starts_with), 1),
                (ERPItem.item_code.ilike(starts_with), 2),
                else_=3,
            ),
            ERPItem.description,
        )
    else:
        query = query.order_by(ERPItem.description)
    items = query.limit(25).all()
    return jsonify([item.to_dict() for item in items])


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@main.route("/export/<int:session_id>/<fmt>")
def export(session_id, fmt):
    if not _current_user():
        if current_app.config.get("ALLOW_LOCAL_LOGIN", True):
            flash("Please sign in to export a session.", "warning")
            return redirect(url_for("main.login"))
        abort(403)
    session = ProcessingSession.query.get_or_404(session_id)
    items = ExtractedItem.query.filter_by(session_id=session_id).all()

    # Collect all item codes in one pass, then load ERP records in a single query
    active_items = [item for item in items if not item.is_skipped]
    codes_needed = list({item.effective_item_code() for item in active_items if item.effective_item_code()})
    erp_by_code = (
        {
            e.item_code: e
            for e in _branch_items_query(session.branch).filter(ERPItem.item_code.in_(codes_needed)).all()
        }
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
    user = _current_user()
    if not user:
        if current_app.config.get("ALLOW_LOCAL_LOGIN", True):
            flash("Please sign in to manage the catalog.", "warning")
            return redirect(url_for("main.login"))
        abort(403)
    page = request.args.get("page", 1, type=int)
    q = request.args.get("q", "").strip()
    branch = _get_branch_for_request()
    query = _branch_items_query(branch)
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(ERPItem.item_code.ilike(like), ERPItem.description.ilike(like))
        )
    pagination = query.order_by(ERPItem.item_code).paginate(page=page, per_page=50)
    item_count = _branch_items_query(branch).count() if branch else 0
    return render_template(
        "catalog.html",
        pagination=pagination,
        q=q,
        item_count=item_count,
        branches=_all_branches(),
        selected_branch=branch,
        is_admin=user.is_admin,
    )


@main.route("/catalog/upload", methods=["POST"])
def catalog_upload():
    """Upload raw ERP export (xlsx/csv) or processed catalog CSV and refresh index."""
    user = _current_user()
    if not user:
        if current_app.config.get("ALLOW_LOCAL_LOGIN", True):
            flash("Please sign in to upload a catalog.", "warning")
            return redirect(url_for("main.login"))
        abort(403)
    branch = _get_branch_for_request()
    if not branch:
        flash("Select a branch before uploading a catalog.", "error")
        return redirect(url_for("main.catalog"))
    if "file" not in request.files:
        flash("No file provided.", "error")
        return redirect(url_for("main.catalog"))

    file = request.files["file"]
    filename = (file.filename or "").lower()
    if not file.filename or not filename.endswith((".csv", ".xlsx", ".xls")):
        flash("Please upload a CSV or Excel file.", "error")
        return redirect(url_for("main.catalog"))

    try:
        incoming = _read_catalog_upload(file)
    except Exception as exc:
        flash(f"Could not parse catalog file: {exc}", "error")
        return redirect(url_for("main.catalog"))

    try:
        df = prepare_catalog_dataframe(incoming)
    except CatalogValidationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.catalog"))

    replace_all = request.form.get("replace_all") == "1"
    try:
        summary = import_catalog_dataframe(
            branch=branch,
            df=df,
            replace_all=replace_all,
            embedding_model=current_app.config["EMBEDDING_MODEL"],
            output_dir=Path(current_app.root_path).parent / "data" / "catalog",
        )
        if summary["vector_count"]:
            embed_msg = f" Vector index built for {summary['catalog_count']} items."
        else:
            embed_msg = (
                " WARNING: vector index is empty - sentence-transformers may not be loaded."
            )
    except Exception as exc:
        logger.warning("Vector index build failed: %s", exc)
        embed_msg = " (Vector index will be built on first match.)"

    flash(
        f"{branch.code} catalog refreshed: {summary['added']} items added, {summary['updated']} items updated, {summary['linked']} items linked to the branch.{embed_msg}",
        "success",
    )
    return redirect(url_for("main.catalog", branch_id=branch.id))

    if replace_all:
        BranchCatalogItem.query.filter_by(branch_id=branch.id).delete()
        db.session.commit()
        _prune_orphan_erp_items()

    valid_rows = []
    for _, row in df.iterrows():
        code = _clean_csv_value(row["item_code"], 100)
        desc = _clean_csv_value(row["description"], 500)
        if code and desc:
            valid_rows.append((code, row))

    all_codes = sorted({code for code, _ in valid_rows})
    existing_map = {}
    if all_codes:
        existing_map = {
            item.item_code: item
            for item in ERPItem.query.filter(ERPItem.item_code.in_(all_codes)).all()
        }
    existing_links = {
        link.erp_item_id
        for link in BranchCatalogItem.query.filter_by(branch_id=branch.id).all()
    }

    added = 0
    updated = 0
    linked = 0
    _CHUNK = 500  # flush to DB in chunks to keep transaction memory bounded
    for i, (code, row) in enumerate(valid_rows):
        existing = existing_map.get(code)
        payload = {
            "description": _clean_csv_value(row.get("description", ""), 500),
            "keywords": _clean_csv_value(row.get("keywords", ""), None),
            "category": _clean_csv_value(row.get("category", row.get("major_description", "")), 100),
            "material_category": _clean_csv_value(row.get("material_category", row.get("major_description", "")), 100),
            "size": _clean_csv_value(row.get("size", ""), 50),
            "length": _clean_csv_value(row.get("length", ""), 20),
            "brand": _clean_csv_value(row.get("brand", ""), 150),
            "normalized_name": _clean_csv_value(row.get("normalized_name", ""), 255),
            "unit_of_measure": _clean_csv_value(row.get("unit_of_measure", "EA"), 50) or "EA",
            "branch_system_id": _clean_csv_value(row.get("branch_system_id", row.get("system_id", branch.code)), 100),
            "ext_description": _clean_csv_value(row.get("ext_description", ""), 500),
            "major_description": _clean_csv_value(row.get("major_description", ""), 255),
            "minor_description": _clean_csv_value(row.get("minor_description", ""), 255),
            "keyword_user_defined": _clean_csv_value(row.get("keyword_user_defined", ""), None),
            "ai_match_text": _clean_csv_value(row.get("ai_match_text", ""), None),
            "last_sold_date": _clean_csv_value(row.get("last_sold_date", ""), 20),
            "days_since_last_sold": row.get("days_since_last_sold") if pd.notna(row.get("days_since_last_sold")) else None,
            "sold_recency_bucket": _clean_csv_value(row.get("sold_recency_bucket", "unknown"), 50) or "unknown",
            "sold_weight": float(row.get("sold_weight", 0.25) or 0.25),
        }

        if existing:
            for key, val in payload.items():
                setattr(existing, key, val)
            existing.embedding = None
            updated += 1
        else:
            item = ERPItem(item_code=code, **payload)
            db.session.add(item)
            db.session.flush()
            existing_map[code] = item
            added += 1
            existing = item

        if existing.id not in existing_links:
            db.session.add(BranchCatalogItem(branch_id=branch.id, erp_item_id=existing.id))
            existing_links.add(existing.id)
            linked += 1

        if (i + 1) % _CHUNK == 0:
            db.session.flush()

    db.session.commit()

    all_items = _branch_items(branch)
    _export_catalog_artifacts()
    try:
        idx = item_matcher.build_index(all_items, current_app.config["EMBEDDING_MODEL"], cache_key=f"branch:{branch.id}")
        if idx is not None and idx.catalog_refs:
            embed_msg = f" Vector index built for {len(all_items)} items."
        else:
            embed_msg = (
                " WARNING: vector index is empty — sentence-transformers may not be loaded."
            )
    except Exception as exc:
        logger.warning("Vector index build failed: %s", exc)
        embed_msg = " (Vector index will be built on first match.)"

    flash(
        f"{branch.code} catalog refreshed: {added} items added, {updated} items updated, {linked} items linked to the branch.{embed_msg}",
        "success",
    )
    return redirect(url_for("main.catalog", branch_id=branch.id))


@main.route("/catalog/delete", methods=["POST"])
def catalog_delete():
    user = _current_user()
    if not user:
        if current_app.config.get("ALLOW_LOCAL_LOGIN", True):
            flash("Please sign in to modify the catalog.", "warning")
            return redirect(url_for("main.login"))
        abort(403)
    branch = _get_branch_for_request()
    if not branch:
        flash("Select a branch before clearing a catalog.", "error")
        return redirect(url_for("main.catalog"))
    BranchCatalogItem.query.filter_by(branch_id=branch.id).delete()
    db.session.commit()
    _prune_orphan_erp_items()
    item_matcher.clear_index(cache_key=f"branch:{branch.id}")
    flash(f"{branch.code} catalog cleared.", "warning")
    return redirect(url_for("main.catalog", branch_id=branch.id))


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

# ---------------------------------------------------------------------------
# Metrics dashboard + API
# ---------------------------------------------------------------------------

@main.route("/metrics")
def metrics_dashboard():
    """Human-readable metrics dashboard."""
    days = request.args.get("days", 30, type=int)
    summary = metrics_service.get_summary(days=days)
    recent = metrics_service.get_recent_sessions(limit=25)
    conf_dist = metrics_service.get_confidence_distribution(days=days)
    provider_stats = metrics_service.get_provider_stats(days=days)
    return render_template(
        "metrics.html",
        summary=summary,
        recent_sessions=recent,
        conf_dist=conf_dist,
        provider_stats=provider_stats,
        days=days,
    )


@main.route("/api/metrics")
def api_metrics():
    """Machine-readable metrics endpoint for agents and monitoring tools.

    Query params:
      days (int, default 30): rolling window for aggregate stats
      sessions (int, default 25): number of recent sessions to include

    Response shape::

        {
          "summary": { ... },          # aggregate stats for the window
          "recent_sessions": [ ... ],  # per-session timing + accuracy
          "confidence_distribution": { ... },  # item count per score bucket
          "provider_stats": { ... },   # per-AI-provider breakdown
        }
    """
    days = request.args.get("days", 30, type=int)
    limit = request.args.get("sessions", 25, type=int)
    return jsonify({
        "summary": metrics_service.get_summary(days=days),
        "recent_sessions": metrics_service.get_recent_sessions(limit=limit),
        "confidence_distribution": metrics_service.get_confidence_distribution(days=days),
        "provider_stats": metrics_service.get_provider_stats(days=days),
    })


@main.route("/health")
def health():
    """Lightweight liveness + readiness probe."""
    branch = _current_branch()
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
            has_catalog = _branch_items_query(branch).with_entities(ERPItem.id).limit(1).first() is not None
        except Exception:
            pass

    # Report vector index health without touching the DB
    vi = item_matcher.get_index(cache_key=f"branch:{branch.id}") if branch else item_matcher.get_index()
    vector_index_items = len(vi.catalog_refs) if vi is not None else 0
    vector_model_loaded = vi.model is not None if vi is not None else False

    status = "ok" if db_ok else "degraded"
    return jsonify({
        "status": status,
        "db": db_ok,
        "catalog_loaded": has_catalog,
        "branch": branch.code if branch else None,
        "branch_count": Branch.query.filter_by(is_active=True).count() if db_ok else 0,
        "anthropic_key_set": bool(current_app.config.get("ANTHROPIC_API_KEY")),
        "openai_key_set": bool(current_app.config.get("OPENAI_API_KEY")),
        "default_ai_provider": current_app.config.get("DEFAULT_AI_PROVIDER", "claude"),
        "vector_index_items": vector_index_items,
        "vector_model_loaded": vector_model_loaded,
    }), 200 if db_ok else 503
