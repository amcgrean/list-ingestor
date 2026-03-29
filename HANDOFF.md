# Agent Handoff: Contractor Language Processing & Match Optimization

## What Was Just Shipped

### Commit 762f3c2 — Industry synonym/abbreviation expansion
Comprehensive pass adding ~30 new abbreviation expansions to `_ABBREVIATIONS` in `app/services/item_matcher.py`:
- **Structural member terms**: joists, rafters, stringers, sleepers, blocking, furring, ledger, mudsill, sill plate, rim joist/board → all inject "lumber" signal so vector encoder distinguishes dimensional lumber from hardware sharing the same name
- **Engineered wood**: i-joist→TJI, LSL→timberstrand, PSL→parallam, microllam→LVL
- **Hardware trade synonyms**: lag bolts→lag screw, through bolts→carriage bolt, spindles→baluster, structural screws→GRK RSS, deck boards→decking, deck screws→exterior fastener, hidden fasteners→camo
- **Species/treatment**: DFL, hem-fir, ACQ, ground contact, above ground
- **Dimension regex**: Extended `_DIM_RE` to match NxN patterns (6x6, 2x10, 4x4) for the +0.06 text bonus
- **Pattern ordering fix**: i-joist and rim joist patterns run before generic joists? to prevent premature matching

### Commit 472970a (prior session) — Section header context fix
- Strengthened Stage A vision prompt with explicit section header handling instructions + concrete example
- Added fallback: `_extract_categories_from_summary()` populates `global_material_context` from document summary when lines lack section headers

### Both deployed to Pi (agility-ai)

---

## The Core Problem: Contractor ↔ Catalog Vocabulary Gap

Contractors write material lists in trade language. The ERP catalog describes the same items by grade/species/dimensions. The vector model (`all-MiniLM-L6-v2`) matches on semantic similarity, but when two domains use different words for the same thing, similarity scores break down.

**Examples from real sessions:**
| Contractor writes | Catalog says | Problem |
|---|---|---|
| "2x10 joists" | "2x10-16' #1 SYP Treated-Ground Contact" | "joist" only in hardware descriptions (joist hanger nail), not lumber |
| "6x6 posts" | "6x6-08' SYP S4S Treated-In Ground" | "post" only in hardware (post anchor, post cap) |
| "lag bolts" | "Hex Lag Screw HDG" | Synonym mismatch |
| "spindles" | "Balusters" | Regional/trade term variation |
| "3-1/8 bronze screws" | "9x3-1/8" Bronze Star Screw-Bulk" | Correct item exists but wrong size matched |
| "treated 9" beam glu 16'" | "3-1/2"x9-1/2"--R/L Treated Glu-Lam Beam" | Matched T&G board instead of glulam |

The abbreviation expansion partially solves this by injecting bridge words. But there are deeper issues to explore.

---

## Directions to Explore

### 1. Material/Finish Discrimination (HIGH IMPACT)

Session 25 items 468-469: "2-1/2" screws Bronze" matched **stainless steel** screws. The correct bronze screws exist (`scrbrzstr2151lb`). The vector model sees "2-1/2 screw" and ranks all 2-1/2" screws similarly regardless of material.

**Ideas:**
- **Negative signal / penalty**: Extract material keywords from query (bronze, stainless, galvanized, aluminum, copper). If query says "bronze" and candidate says "stainless", apply a penalty factor (e.g., -0.15 to confidence).
- **Material keyword sets**: Build a dict of mutually-exclusive material groups:
  ```python
  _MATERIAL_GROUPS = {
      "bronze": {"bronze", "brz"},
      "stainless": {"stainless", "ss", "stainless steel"},
      "galvanized": {"galvanized", "hdg", "hot dip galvanized", "galv"},
      "aluminum": {"aluminum", "aluminium", "alum"},
      "copper": {"copper"},
  }
  ```
  If query's material group ≠ candidate's material group → penalty. If they match → bonus.
- **Same approach for wood species**: treated vs fir vs cedar vs SPF — if query says "treated" and catalog says "Fir", penalize.

### 2. Contextual Expansion Based on Document Type (MEDIUM IMPACT)

Session 25 had zero `upload_context` — the system had no idea this was a deck project. When context IS available ("deck project", "interior framing", "siding job"), the expansion should change:
- "joists" on a deck → treated SYP, "joists" on interior → fir/SPF
- "posts" on a deck → treated 4x4/6x6, "posts" on a fence → treated 4x4

**Ideas:**
- **Context-aware abbreviation expansion**: Instead of static `_ABBREVIATIONS`, have a function that takes `upload_context` and returns a modified abbreviation dict. E.g., if context contains "deck":
  ```python
  r"\bjoists?\b": "lumber joist treated SYP",
  r"\bposts?\b": "lumber post treated SYP",
  ```
  If context contains "framing" or "interior":
  ```python
  r"\bjoists?\b": "lumber joist fir SPF",
  r"\bstuds?\b": "lumber stud fir SPF",
  ```
- **Prompt the upload form to require context**: Make "Project Type" (deck, framing, siding, trim, etc.) a required dropdown on upload. This becomes the strongest signal for disambiguation.

### 3. Catalog-Side Enrichment (MEDIUM IMPACT)

The catalog descriptions are bare: "2x10-16' #1 SYP Treated-Ground Contact" doesn't mention "joist", "rafter", "rim board", or any usage term. We can bulk-enrich `keywords` or `ai_match_text` to include common usage terms:

**Ideas:**
- **Rule-based keyword injection into catalog items:**
  ```python
  # For all 2x8, 2x10, 2x12 treated items, add:
  "joist rafter header rim board deck framing structural"
  # For all 4x4, 6x6 treated items, add:
  "post column support pier deck"
  # For all 2x4, 2x6 items (fir/SPF), add:
  "stud framing wall plate"
  # For all 5/4x6 decking items, add:
  "deck board decking surface"
  ```
- **Run as a one-time migration script** (`scripts/enrich_catalog_keywords.py`) that parses the item_code and description to determine size/species, then appends usage keywords.
- **Then rebuild ai_match_text** with `rebuild_ai_match.py`.
- This is the **dual-side** approach: expand contractor queries AND enrich catalog descriptions, so the vector space has overlap from both directions.

### 4. OCR Error Recovery (LOW-MEDIUM IMPACT)

Session 25 item 464: "1os 210 hangers (50)" — OCR read "LUS" as "1os". The normalized description became "210 Hangers" which lost the brand prefix entirely.

**Ideas:**
- **Common OCR substitution table**: `1` ↔ `l` ↔ `I`, `0` ↔ `O`, `5` ↔ `S`, etc. When a token doesn't match any known abbreviation, try character substitutions and re-check.
- **Hardware model prefix detection**: If token before a number looks like a garbled Simpson model prefix (LUS, LU, HU, MIU, LSU, etc.), try the closest valid prefix.
- **Levenshtein pre-filter on abbreviations**: Before matching, check if any unknown token is within edit-distance 1 of a known abbreviation key.

### 5. Size/Dimension Weighting (MEDIUM IMPACT)

Session 25 item 469: "3-1/8" screws Bronze" matched "9x1-1/2" Bronze Star Screw-Bulk" instead of "9x3-1/8" Bronze Star Screw-Bulk". Both are bronze, but the size is wrong.

The `_dimension_text_bonus` (+0.06) exists but may not be strong enough, and it only fires when the exact dimension string appears. "3-1/8" would need to appear literally in the candidate text.

**Ideas:**
- **Increase dimension bonus from 0.06 to 0.10-0.12** — dimensions are often the single most important discriminator for hardware.
- **Add a dimension MISMATCH penalty**: If query has dimension "3-1/8" and candidate has a different dimension "1-1/2", apply -0.08. This pushes wrong-size items down.
- **Parse dimensions into numeric values for proximity scoring**: Convert "3-1/8" → 3.125, "1-1/2" → 1.5, "2-1/2" → 2.5. Then score by how close the numeric values are.

---

## Session 25 Full Diagnosis (for reference)

| ID | Raw | Matched | Score | Problem |
|----|-----|---------|-------|---------|
| 462 | 2 6x6 + Posts | RDI Elevations Mid-Post 2x4-36" | 0.52 | "post" matched hardware, not 6x6 treated lumber. Catalog HAS 6x6 treated. |
| 463 | 2x10 Joists 25 | 10dx1 1/2" HDG Joist Hanger Nail | 0.36 | "joist" pulled toward hardware. Catalog HAS 2x10 treated. |
| 464 | 1os 210 hangers (50) | TUS24 Mitek Undersaddle Hanger | 0.39 | OCR: "1os" = "LUS". LUS210 NOT in catalog (gap). |
| 465 | treated 9" Beam Glu 16' | 2x8-16' T&G #1 SYP .40 Treated | 0.51 | Glulam beam IS in catalog (`9treglulam35`). Match failed on description format. |
| 466 | 100 trex 16' decking, trowel sand | Trex Enhance 5/4x6-16' GRV Toasted-Sand | 0.66 | "Trowel Sand" not in catalog. Closest is "Toasted Sand". Likely customer error or catalog gap. |
| 467 | Decking Clips | Ninja Hidden Deck Clip 50-Sq | 0.68 | Reasonable match. |
| 468 | 2-1/2" screws Bronze etc | 9x2-1/2" Stainless Steel Star Screw-lb | 0.57 | **Correct item exists** (`scrbrzstr2151lb`). Stainless outscored bronze — material discrimination failure. |
| 469 | 3-1/8" screws Bronze * | 9x1-1/2" Bronze Star Screw-Bulk | 0.56 | **Correct item exists** (`scrbrzstr318`). Wrong size — dimension weighting too weak. |

---

## Remaining Backlog (from prior sessions)

| Pri | Item | Status |
|-----|------|--------|
| 🟡 P2 | Add color column to DB and re-import from catalog | Pending |
| 🟠 P3 | Metrics page mobile table (14 columns) | Pending |
| 🟠 P3 | Catalog page mobile form | Pending |
| 🟠 P3 | Async upload UX (browser timeout on mobile — needs progress polling instead of sync wait) | Pending |
| 🟢 P4 | Proactive vector index rebuild after catalog upload | Pending |
| 🟢 P4 | Move `rebuild_ai_match.py` into `scripts/` | Pending |

## Key Files

- `app/services/item_matcher.py` — `_ABBREVIATIONS` dict, `_DIM_RE`, `match_items_batch()`, `_dimension_text_bonus()`, `_COLOR_TO_BRAND`
- `app/services/vision_extract_service.py` — Stage A prompt, `_extract_categories_from_summary()`, `_extract_document_payload()`
- `app/services/upload_context.py` — `enrich_description_for_matching()`, `normalize_document_context()`
- `app/services/context_interpreter.py` — Stage B context resolution
- `app/services/parse_pipeline.py` — Stage C match preparation
- `app/routes.py` — process/reprocess/save endpoints

## DB Access

```bash
ssh agility-ai "cd /home/amcgrean/services/list-ingestor && docker compose exec -T db psql -U erp -d erp -c \"YOUR SQL HERE\""
```
