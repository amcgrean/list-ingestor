from app import db
from datetime import datetime
import json


class IngesterMetrics(db.Model):
    """Per-session pipeline timing, volume, and error metrics captured at processing time.

    Each row corresponds to one ProcessingSession upload.  Accuracy metrics
    (correction / skip rates) are derived from MatchFeedbackEvent records after
    the user completes review — this table captures what was measurable at
    ingest time: stage latencies, item counts, and initial confidence scores.
    """
    __tablename__ = "ingester_metrics"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(
        db.Integer, db.ForeignKey("processing_sessions.id"),
        unique=True, nullable=False, index=True,
    )
    ai_provider = db.Column(db.String(20), default="claude")

    # Stage durations (milliseconds)
    ocr_duration_ms = db.Column(db.Integer, nullable=True)
    ai_parse_duration_ms = db.Column(db.Integer, nullable=True)
    match_duration_ms = db.Column(db.Integer, nullable=True)
    total_duration_ms = db.Column(db.Integer, nullable=True)

    # Volume
    items_extracted = db.Column(db.Integer, default=0)
    items_matched = db.Column(db.Integer, default=0)
    items_below_threshold = db.Column(db.Integer, default=0)

    # Score averages at match time (before any user correction)
    avg_confidence = db.Column(db.Float, nullable=True)
    avg_fuzzy_score = db.Column(db.Float, nullable=True)
    avg_vector_score = db.Column(db.Float, nullable=True)

    # Error flags
    ocr_error = db.Column(db.Boolean, default=False)
    ai_parse_error = db.Column(db.Boolean, default=False)
    match_error = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    session = db.relationship("ProcessingSession", backref=db.backref("metrics", uselist=False))

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "ai_provider": self.ai_provider,
            "ocr_duration_ms": self.ocr_duration_ms,
            "ai_parse_duration_ms": self.ai_parse_duration_ms,
            "match_duration_ms": self.match_duration_ms,
            "total_duration_ms": self.total_duration_ms,
            "items_extracted": self.items_extracted,
            "items_matched": self.items_matched,
            "items_below_threshold": self.items_below_threshold,
            "avg_confidence": round(self.avg_confidence, 4) if self.avg_confidence is not None else None,
            "avg_fuzzy_score": round(self.avg_fuzzy_score, 4) if self.avg_fuzzy_score is not None else None,
            "avg_vector_score": round(self.avg_vector_score, 4) if self.avg_vector_score is not None else None,
            "ocr_error": self.ocr_error,
            "ai_parse_error": self.ai_parse_error,
            "match_error": self.match_error,
            "created_at": self.created_at.isoformat(),
        }


class ERPItem(db.Model):
    """An item in the ERP catalog loaded from CSV."""
    __tablename__ = "erp_items"

    id = db.Column(db.Integer, primary_key=True)
    item_code = db.Column(db.String(100), unique=True, nullable=False, index=True)
    description = db.Column(db.String(500), nullable=False)
    keywords = db.Column(db.Text, default="")
    category = db.Column(db.String(100), default="")
    unit_of_measure = db.Column(db.String(50), default="EA")
    material_category = db.Column(db.String(100), default="")
    size = db.Column(db.String(50), default="")
    length = db.Column(db.String(20), default="")
    brand = db.Column(db.String(150), default="")
    normalized_name = db.Column(db.String(255), default="")
    branch_system_id = db.Column(db.String(100), default="", index=True)
    ext_description = db.Column(db.String(500), default="")
    major_description = db.Column(db.String(255), default="")
    minor_description = db.Column(db.String(255), default="")
    keyword_user_defined = db.Column(db.Text, default="")
    ai_match_text = db.Column(db.Text, default="")
    last_sold_date = db.Column(db.String(20), default="")
    days_since_last_sold = db.Column(db.Integer, nullable=True)
    sold_recency_bucket = db.Column(db.String(50), default="unknown")
    sold_weight = db.Column(db.Float, default=0.25)
    # Serialized embedding vector (list of floats as JSON string)
    _embedding = db.Column("embedding", db.Text, nullable=True)

    @property
    def embedding(self):
        if self._embedding:
            return json.loads(self._embedding)
        return None

    @embedding.setter
    def embedding(self, value):
        if value is not None:
            self._embedding = json.dumps(value)
        else:
            self._embedding = None

    @property
    def searchable_text(self):
        """Combined text used for matching."""
        parts = [self.description]
        if self.keywords:
            parts.append(self.keywords)
        if self.category:
            parts.append(self.category)
        if self.material_category:
            parts.append(self.material_category)
        if self.size:
            parts.append(self.size)
        if self.length:
            parts.append(f"{self.length}ft")
        if self.brand:
            parts.append(self.brand)
        if self.ai_match_text:
            parts.append(self.ai_match_text)
        elif self.normalized_name:
            parts.append(self.normalized_name)
        return " ".join(parts)

    @property
    def sku(self):
        return self.item_code

    def to_dict(self):
        return {
            "id": self.id,
            "item_code": self.item_code,
            "description": self.description,
            "keywords": self.keywords,
            "category": self.category,
            "unit_of_measure": self.unit_of_measure,
            "material_category": self.material_category,
            "size": self.size,
            "length": self.length,
            "brand": self.brand,
            "normalized_name": self.normalized_name,
            "branch_system_id": self.branch_system_id,
            "ext_description": self.ext_description,
            "major_description": self.major_description,
            "minor_description": self.minor_description,
            "keyword_user_defined": self.keyword_user_defined,
            "ai_match_text": self.ai_match_text,
            "last_sold_date": self.last_sold_date,
            "days_since_last_sold": self.days_since_last_sold,
            "sold_recency_bucket": self.sold_recency_bucket,
            "sold_weight": self.sold_weight,
        }


class Branch(db.Model):
    __tablename__ = "branches"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "is_active": self.is_active,
        }


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(255), default="")
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    default_branch_id = db.Column(db.Integer, db.ForeignKey("branches.id"), nullable=True)
    last_seen_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    default_branch = db.relationship("Branch", foreign_keys=[default_branch_id])

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "full_name": self.full_name,
            "is_admin": self.is_admin,
            "is_active": self.is_active,
            "default_branch_id": self.default_branch_id,
            "default_branch_code": self.default_branch.code if self.default_branch else None,
        }


class BranchCatalogItem(db.Model):
    __tablename__ = "branch_catalog_items"

    id = db.Column(db.Integer, primary_key=True)
    branch_id = db.Column(db.Integer, db.ForeignKey("branches.id"), nullable=False, index=True)
    erp_item_id = db.Column(db.Integer, db.ForeignKey("erp_items.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    branch = db.relationship("Branch", backref=db.backref("catalog_links", cascade="all, delete-orphan"))
    erp_item = db.relationship("ERPItem", backref=db.backref("branch_links", cascade="all, delete-orphan"))

    __table_args__ = (
        db.UniqueConstraint("branch_id", "erp_item_id", name="uq_branch_catalog_item"),
    )


class ItemAlias(db.Model):
    """User learned alias-to-SKU mapping from review overrides."""
    __tablename__ = "item_aliases"

    id = db.Column(db.Integer, primary_key=True)
    alias = db.Column(db.String(255), unique=True, nullable=False, index=True)
    sku = db.Column(db.String(100), nullable=False, index=True)
    usage_count = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class MatchFeedbackEvent(db.Model):
    """Historical user feedback captured during review to improve matching."""
    __tablename__ = "match_feedback_events"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("processing_sessions.id"), nullable=False, index=True)
    extracted_item_id = db.Column(db.Integer, db.ForeignKey("extracted_items.id"), nullable=False, index=True)
    raw_description = db.Column(db.String(500), nullable=False)
    normalized_description = db.Column(db.String(255), nullable=False, index=True)
    predicted_sku = db.Column(db.String(100), nullable=True, index=True)
    final_sku = db.Column(db.String(100), nullable=True, index=True)
    was_corrected = db.Column(db.Boolean, default=False, nullable=False, index=True)
    was_skipped = db.Column(db.Boolean, default=False, nullable=False, index=True)
    confidence_score = db.Column(db.Float, default=0.0, nullable=False)
    fuzzy_score = db.Column(db.Float, default=0.0, nullable=False)
    vector_score = db.Column(db.Float, default=0.0, nullable=False)
    feedback_comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True, server_default=db.func.now())


class SessionFeedbackEvent(db.Model):
    """User feedback event for an entire session and optional reprocess request."""
    __tablename__ = "session_feedback_events"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("processing_sessions.id"), nullable=False, index=True)
    comment = db.Column(db.Text, nullable=False)
    requested_reprocess = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)


class ProcessingSession(db.Model):
    """Tracks one upload-and-process job."""
    __tablename__ = "processing_sessions"

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    file_type = db.Column(db.String(20), nullable=False)  # jpg/png/pdf
    branch_id = db.Column(db.Integer, db.ForeignKey("branches.id"), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    raw_ocr_text = db.Column(db.Text, default="")
    status = db.Column(db.String(50), default="pending")
    # pending / ocr_complete / parsed / matched / reviewed / exported
    error_message = db.Column(db.Text, nullable=True)
    system_id = db.Column(db.String(100), default="", index=True)
    session_comment = db.Column(db.Text, nullable=True)
    feedback_reprocess_requested = db.Column(
        db.Boolean,
        default=False,
        nullable=False,
        server_default=db.text("false"),
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    items = db.relationship("ExtractedItem", backref="session", cascade="all, delete-orphan")
    branch = db.relationship("Branch")
    user = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "filename": self.filename,
            "file_type": self.file_type,
            "branch_code": self.branch.code if self.branch else None,
            "user_email": self.user.email if self.user else None,
            "status": self.status,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat(),
            "item_count": len(self.items),
        }


class ExtractedItem(db.Model):
    """One line item extracted from an uploaded material list."""
    __tablename__ = "extracted_items"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("processing_sessions.id"), nullable=False)

    # From AI parser
    quantity = db.Column(db.Float, default=1.0)
    raw_description = db.Column(db.String(500), nullable=False)

    # From item matcher
    matched_item_code = db.Column(db.String(100), nullable=True)
    matched_description = db.Column(db.String(500), nullable=True)
    confidence_score = db.Column(db.Float, default=0.0)
    fuzzy_score = db.Column(db.Float, default=0.0)
    vector_score = db.Column(db.Float, default=0.0)

    # User edits
    final_quantity = db.Column(db.Float, nullable=True)
    final_item_code = db.Column(db.String(100), nullable=True)
    is_confirmed = db.Column(db.Boolean, default=False)
    is_skipped = db.Column(db.Boolean, default=False)

    def effective_quantity(self):
        return self.final_quantity if self.final_quantity is not None else self.quantity

    def effective_item_code(self):
        return self.final_item_code or self.matched_item_code

    def to_dict(self):
        erp_item = None
        if self.effective_item_code():
            erp_item = ERPItem.query.filter_by(item_code=self.effective_item_code()).first()
        return {
            "id": self.id,
            "quantity": self.effective_quantity(),
            "raw_description": self.raw_description,
            "matched_item_code": self.matched_item_code,
            "matched_description": self.matched_description,
            "confidence_score": round(self.confidence_score, 3),
            "fuzzy_score": round(self.fuzzy_score, 3),
            "vector_score": round(self.vector_score, 3),
            "final_quantity": self.final_quantity,
            "final_item_code": self.final_item_code,
            "is_confirmed": self.is_confirmed,
            "is_skipped": self.is_skipped,
            "erp_description": erp_item.description if erp_item else self.matched_description,
        }
