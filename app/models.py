from app import db
from datetime import datetime
import json


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
        if self.normalized_name:
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
        }


class ItemAlias(db.Model):
    """User learned alias-to-SKU mapping from review overrides."""
    __tablename__ = "item_aliases"

    id = db.Column(db.Integer, primary_key=True)
    alias = db.Column(db.String(255), unique=True, nullable=False, index=True)
    sku = db.Column(db.String(100), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ProcessingSession(db.Model):
    """Tracks one upload-and-process job."""
    __tablename__ = "processing_sessions"

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    file_type = db.Column(db.String(20), nullable=False)  # jpg/png/pdf
    raw_ocr_text = db.Column(db.Text, default="")
    status = db.Column(db.String(50), default="pending")
    # pending / ocr_complete / parsed / matched / reviewed / exported
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    items = db.relationship("ExtractedItem", backref="session", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "filename": self.filename,
            "file_type": self.file_type,
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
