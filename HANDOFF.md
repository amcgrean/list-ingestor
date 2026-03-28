# Agent Handoff: Learning Pipeline

## What Was Just Shipped (commit 32cf099)

Brand/color scoring was completely dead — `parsed_brands`/`parsed_colors` were never passed to `match_items_batch()` in either the process or reprocess routes. Fixed. Also: 55 color-to-brand mappings, 79 SKU abbreviation expansions, section header flowing into match_text, reprocess using enriched descriptions, color column on ERPItem, mobile UX fixes.

## Post-Deploy Tasks (run on Pi after `docker-compose up --build`)

1. `docker exec -it <container> python scripts/rebuild_ai_match.py` — regenerates ai_match_text + color for all catalog items
2. `curl -X POST http://localhost:8000/admin/clean-nan-brands -H 'Cookie: <admin_session>'` — cleans "nan" brand strings from DB

---

## The Problem

Every user correction is a labeled training example: "this raw text maps to this SKU." The system captures corrections (MatchFeedbackEvent, ItemAlias) but uses them weakly:

- **ItemAlias** is exact-match only — "Cinn Cove 1x6 16" gets no benefit from a correction on "Cinnamon Cove 1x6 16ft"
- **Feedback rerank** is capped at +0.2 boost and only matches on exact `normalized_description`
- **No re-parse** — uploaded files are deleted after parsing, so bad OCR/LLM parse errors can't be fixed
- **No field-level corrections** — users can fix the matched SKU but not brand/color/section_header
- **No customer-specific learning** — one customer's "CC" (Cinnamon Cove) overwrites another's "CC" (cedar closet)

## Architecture Overview

```
Upload → Stage A (VisionExtractService) → Stage B (ContextInterpreter) → Stage C (parse_pipeline)
  → match_items_batch (item_matcher) → Review page → Save (creates MatchFeedbackEvent + ItemAlias)
  → Reprocess (re-runs matching only, reads back feedback via _feedback_counts_batch)
```

Key files:
- `app/services/vision_extract_service.py` — Stage A: OCR/LLM extraction
- `app/services/context_interpreter.py` — Stage B: LLM context resolution (section headers, brand/color)
- `app/services/parse_pipeline.py` — Stage C: prepare MatchReadyLine with match_text
- `app/services/item_matcher.py` — Matching: vector + fuzzy + brand/color bonuses + feedback rerank
- `app/routes.py` — HTTP endpoints for process, reprocess, save, feedback-workflow
- `app/models.py` — ERPItem, ExtractedItem, ItemAlias, MatchFeedbackEvent, etc.

---

## 5 Gaps to Fix (in priority order)

### Gap 1: Re-Parse Option (HIGH priority, MEDIUM effort)

**Problem:** Reprocess only re-runs matching. If OCR/LLM parse was bad (wrong quantity, missed header, wrong brand), can't fix it. Files deleted after parsing (`routes.py` ~line 640).

**Implementation:**

1. **Save uploaded files permanently:**
   - In the upload processing route (`routes.py`, around line 440-460), after saving temp files, copy them to `uploads/sessions/{session_id}/`
   - Store the file paths list in a new `ProcessingSession.uploaded_files_json` column (JSON array of filenames)
   - Do NOT delete originals after parsing

2. **New endpoint `POST /review/<session_id>/reparse`:**
   ```python
   @main.route("/review/<int:session_id>/reparse", methods=["POST"])
   def reparse_session(session_id):
       session = ProcessingSession.query.get_or_404(session_id)
       # Load saved files from uploads/sessions/{session_id}/
       file_paths = [Path(f) for f in json.loads(session.uploaded_files_json or "[]")]

       # Build enriched upload_context from:
       # - Original upload_context
       # - Session comment (user feedback)
       # - Summary of corrections made so far
       corrections = MatchFeedbackEvent.query.filter_by(
           session_id=session_id, was_corrected=True
       ).all()
       correction_summary = "; ".join(
           f'"{c.raw_description}" should be {c.final_sku}'
           for c in corrections[:20]  # cap to avoid prompt overflow
       )
       upload_ctx = f"{session.upload_context or ''} User corrections: {correction_summary}".strip()

       # Re-run full pipeline (A/B/C + matching)
       _, _, stage_c_lines, doc_context = parse_uploads(file_paths, api_key, session.id, upload_ctx)
       # ... rebuild parsed_items, run match_items_batch with parsed_brands/parsed_colors
       # ... update ExtractedItem rows, preserve user-confirmed items
   ```

3. **UI:** Add "Re-Parse" button next to "Reprocess" in `review.html`/`review.js`

4. **Files to modify:** `models.py` (add `uploaded_files_json` to ProcessingSession), `routes.py` (file save + new endpoint), `review.html`/`review.js` (button)

---

### Gap 2: Per-Field Parse Corrections (HIGH priority, MEDIUM effort)

**Problem:** Users can only correct the matched SKU. Brand, color, section_header, product_family are frozen after Stage B. No way to say "this was Trex, not TimberTech."

**Implementation:**

1. **New model `ParseCorrectionEvent`:**
   ```python
   class ParseCorrectionEvent(db.Model):
       __tablename__ = "parse_correction_events"
       id = db.Column(db.Integer, primary_key=True)
       session_id = db.Column(db.Integer, db.ForeignKey("processing_sessions.id"), nullable=False, index=True)
       extracted_item_id = db.Column(db.Integer, db.ForeignKey("extracted_items.id"), nullable=False)
       field_name = db.Column(db.String(50), nullable=False)  # brand, color, section_header, product_family
       original_value = db.Column(db.String(255), default="")
       corrected_value = db.Column(db.String(255), default="")
       created_at = db.Column(db.DateTime, default=datetime.utcnow)
   ```

2. **Make fields editable in review UI:**
   - Add small edit icons next to brand/color/section_header/product_family display
   - On click, turn into inline text input
   - Collect corrections in the same save payload
   - In `save_review()`, update `ExtractedItem.brand`, `.color`, etc. and create `ParseCorrectionEvent`

3. **Feed corrections into reparse:**
   - When reparse runs, load all `ParseCorrectionEvent` rows for the session
   - After Stage B interpretation, override specific item fields with user corrections
   - This ensures the user's knowledge persists across re-parses

4. **Files to modify:** `models.py`, `routes.py` (save_review), `review.html`, `review.js`, `__init__.py` (add to _sync_table_columns)

---

### Gap 3: Fuzzy Alias Matching (HIGH priority, MEDIUM effort)

**Problem:** ItemAlias lookup is exact on `normalise_description(raw_text)`. No cross-description generalization.

**Implementation:**

1. **Embedding-based alias index:**
   - New class `AliasIndex` in `item_matcher.py` (similar to VectorIndex but for aliases)
   - When an alias is created/updated in `save_review()`, also encode it with sentence-transformers
   - Store embedding in a new `ItemAlias.embedding` column (JSON text, same pattern as ERPItem._embedding)
   - Build a small FAISS index of all alias embeddings (much smaller than catalog — hundreds not thousands)

2. **Fallback lookup flow:**
   ```python
   def _alias_lookup_batch(descriptions):
       # Step 1: exact match (existing)
       exact = {norm: sku for norm, sku in exact_query}

       # Step 2: for unresolved, try embedding similarity
       unresolved = [d for d in descriptions if normalise_description(d) not in exact]
       if unresolved and alias_index:
           for desc in unresolved:
               hits = alias_index.search(desc, k=1)
               if hits and hits[0].score > 0.92:
                   exact[normalise_description(desc)] = hits[0].sku
                   # Return with confidence 0.95, not 1.0
       return exact
   ```

3. **Alias usage_count as confidence multiplier:**
   - In `match_item_candidates` and `match_items_batch`, when alias resolves:
     - count >= 3: confidence 1.0 (well-established)
     - count == 2: confidence 0.95
     - count == 1: confidence 0.90 (single correction, might be wrong)
   - Prevents one bad correction from cementing forever

4. **Files to modify:** `models.py` (ItemAlias.embedding), `item_matcher.py` (AliasIndex class, modified lookup functions), `routes.py` (encode alias on save)

---

### Gap 4: Correction Pattern Mining (MEDIUM priority, LOW effort)

**Problem:** Users keep making the same type of correction but the system doesn't learn abbreviation patterns from them.

**Implementation:**

1. **New script `scripts/mine_correction_patterns.py`:**
   ```python
   # 1. Query MatchFeedbackEvent WHERE was_corrected=True
   # 2. For each correction, tokenize raw_description and final SKU's catalog description
   # 3. Find tokens in user text that don't appear in the normalized form
   #    but the corrected SKU's description has a similar token
   # 4. Cluster: if "TT" appears in 5+ corrections and always maps to SKUs
   #    containing "tiki torch", suggest: r"\btt\b": "tiki torch"
   # 5. Output as JSON suggestions for human review
   ```

2. **Also mine ParseCorrectionEvent (from Gap 2):**
   - If users frequently change brand from "" to "Trex" on items containing "transcend", suggest adding a pattern

3. **Run schedule:** Admin endpoint `GET /admin/mining-suggestions` or cron job

4. **Files to create:** `scripts/mine_correction_patterns.py`

---

### Gap 5: Customer-Scoped Aliases (MEDIUM priority, MEDIUM effort)

**Problem:** Different customers use different abbreviations. "CC" means Cinnamon Cove to one customer, cedar closet to another. Global aliases can't handle this.

**Implementation:**

1. **Extend ItemAlias:**
   ```python
   customer_context_id = db.Column(db.Integer, db.ForeignKey("customer_job_contexts.id"), nullable=True, index=True)
   ```
   - NULL = global alias (current behavior)
   - Non-NULL = customer-scoped alias

2. **Scoped alias creation (in save_review):**
   - If the session has a matched customer context (session.matched_context_json contains a customer_context_id), create the alias with that scope
   - Also create/update global alias (for general learning)

3. **Scoped alias lookup (in _alias_lookup_batch):**
   - Accept optional `customer_context_id` parameter
   - Query: customer-scoped aliases first (exact match), then global aliases
   - Customer scope wins if both exist

4. **Files to modify:** `models.py` (ItemAlias FK), `routes.py` (save_review, process, reprocess — pass customer context ID through), `item_matcher.py` (_alias_lookup functions)

---

## Testing Checklist

After implementing each gap:

- [ ] Upload a material list with intentionally bad section headers → verify Re-Parse fixes them (Gap 1)
- [ ] Correct brand/color on review page → verify corrections persist and feed into reprocess (Gap 2)
- [ ] Correct "Cinn Cove 1x6" to SKU-X → upload "Cinnamon Cove 1x6 16ft" → verify alias fuzzy-matches (Gap 3)
- [ ] Make 5+ corrections of same type → run mining script → verify it suggests the pattern (Gap 4)
- [ ] Set up two customer contexts with conflicting abbreviations → verify correct scoping (Gap 5)
