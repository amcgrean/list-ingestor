"""Vector index service for SKU semantic retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List

import numpy as np

try:
    import faiss  # type: ignore
except Exception:  # pragma: no cover - exercised by fallback paths
    faiss = None

from sentence_transformers import SentenceTransformer


@dataclass
class VectorHit:
    sku: str
    score: float


class VectorIndex:
    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2"):
        self.model_name = model_name
        self.model = SentenceTransformer(model_name)
        self.faiss_index = None
        self.catalog_refs: list = []
        self.catalog_matrix: np.ndarray | None = None

    def build_index(self, catalog: Iterable):
        self.catalog_refs = list(catalog)
        if not self.catalog_refs:
            self.faiss_index = None
            self.catalog_matrix = None
            return

        texts = [item.searchable_text for item in self.catalog_refs]
        matrix = self.model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
        self.catalog_matrix = matrix.astype("float32")

        if faiss is not None:
            self.faiss_index = faiss.IndexFlatIP(self.catalog_matrix.shape[1])
            self.faiss_index.add(self.catalog_matrix)

    def search(self, query: str, k: int = 5) -> List[VectorHit]:
        if not self.catalog_refs:
            return []

        q_vec = self.model.encode([query], convert_to_numpy=True, normalize_embeddings=True).astype("float32")

        if self.faiss_index is not None:
            scores, idx = self.faiss_index.search(q_vec, min(k, len(self.catalog_refs)))
            hits = []
            for score, i in zip(scores[0], idx[0]):
                if i < 0:
                    continue
                hits.append(VectorHit(sku=self.catalog_refs[i].sku, score=float(max(0.0, score))))
            return hits

        sims = (self.catalog_matrix @ q_vec[0]).tolist() if self.catalog_matrix is not None else []
        ordered = sorted(enumerate(sims), key=lambda x: x[1], reverse=True)[:k]
        return [
            VectorHit(sku=self.catalog_refs[i].sku, score=float(max(0.0, score)))
            for i, score in ordered
        ]
