"""Vector index service for SKU semantic retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List

import numpy as np

try:
    import faiss  # type: ignore
except Exception:  # pragma: no cover - exercised by fallback paths
    faiss = None

try:
    from sentence_transformers import SentenceTransformer  # type: ignore
except Exception:  # pragma: no cover - not installed in lightweight deployments
    SentenceTransformer = None  # type: ignore


@dataclass
class VectorHit:
    sku: str
    score: float


class VectorIndex:
    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2"):
        self.model_name = model_name
        self.model = SentenceTransformer(model_name) if SentenceTransformer is not None else None
        self.faiss_index = None
        self.catalog_refs: list = []
        self.catalog_matrix: np.ndarray | None = None

    def build_index(self, catalog: Iterable):
        # Materialise once; extract only what we need so ORM objects can be GC'd
        items = list(catalog)
        if not items or self.model is None:
            self.faiss_index = None
            self.catalog_matrix = None
            self.catalog_refs = []
            return

        texts = [item.searchable_text for item in items]
        # Store only SKU strings — not heavy ORM objects — so the index doesn't
        # prevent garbage collection of the full ERPItem objects after each request.
        self.catalog_refs = [item.sku for item in items]
        matrix = self.model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
        self.catalog_matrix = matrix.astype("float32")

        if faiss is not None:
            self.faiss_index = faiss.IndexFlatIP(self.catalog_matrix.shape[1])
            self.faiss_index.add(self.catalog_matrix)

    def search(self, query: str, k: int = 5) -> List[VectorHit]:
        return self.search_batch([query], k=k)[0]

    def search_batch(self, queries: List[str], k: int = 5) -> List[List[VectorHit]]:
        """Encode all queries in one forward pass then search for each.

        Much more CPU-efficient than calling search() N times because the
        sentence-transformer can batch-process all queries together.
        """
        if not self.catalog_refs or not queries or self.model is None:
            return [[] for _ in queries]

        # Single encode call for all queries — the transformer batches them
        q_vecs = self.model.encode(
            queries, convert_to_numpy=True, normalize_embeddings=True
        ).astype("float32")

        n = min(k, len(self.catalog_refs))
        results: List[List[VectorHit]] = []

        for q_vec in q_vecs:
            q_vec = q_vec.reshape(1, -1)
            if self.faiss_index is not None:
                scores, idx = self.faiss_index.search(q_vec, n)
                hits = [
                    VectorHit(sku=self.catalog_refs[i], score=float(max(0.0, score)))
                    for score, i in zip(scores[0], idx[0])
                    if i >= 0
                ]
            elif self.catalog_matrix is not None:
                sims = (self.catalog_matrix @ q_vec[0]).tolist()
                ordered = sorted(enumerate(sims), key=lambda x: x[1], reverse=True)[:k]
                hits = [
                    VectorHit(sku=self.catalog_refs[i], score=float(max(0.0, score)))
                    for i, score in ordered
                ]
            else:
                hits = []
            results.append(hits)

        return results
