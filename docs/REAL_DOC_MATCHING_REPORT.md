# Real-Document Matching Test Report — 2026-09-02

Six real handwritten material lists were transcribed into fixtures
(`tests/fixtures/real_docs/`) and replayed through the production matching
path (`enrich_description_for_matching` + `item_matcher.match_items_batch`)
using `scripts/match_real_docs.py`. Full per-line output with candidates is in
`docs/real_doc_match_results.json`.

## Setup

| | |
|---|---|
| Documents | 417 Oakwood deck package (20 lines), Espelund deck takeoff (40), Beisser MoistureShield order (8), Earl exterior trim (8), Steve Hick doors/trim (11), built-ins rooms list (6) |
| Catalog | `example_catalog.csv` — 41 demo items, deck-oriented |
| Weights | fuzzy 0.4 / vector 0.6 (production defaults) |
| Model | all-MiniLM-L6-v2 via a numerically-equivalent ONNX export (huggingface.co is blocked in the test environment; same weights, same mean-pooling + L2 norm) |

**Caveat:** the demo catalog is ~2 orders of magnitude smaller than the
production Beisser catalog, so absolute confidences are depressed and
catalog-gap effects are amplified. The *relative* behaviors below are what
matter.

## Headline numbers

| Document | strong ≥0.75 | review 0.55–0.75 | weak <0.55 |
|---|---|---|---|
| espelund_deck (40) | 4 | 12 | 24 |
| oakwood_deck (20) | 1 | 4 | 15 |
| beisser_moistureshield (8) | 0 | 1 | 7 |
| earl_trim_boards (8) | 0 | 0 | 8 |
| steve_hick_doors (11) | 0 | 0 | 11 |
| builtins_rooms (6) | 0 | 1 | 5 |

Out-of-domain documents (trim, doors, built-ins) correctly bottom out — the
low-confidence signal works. In-domain lines that have a real counterpart in
the catalog score well.

## What the new matcher machinery got right

- **Abbreviation/synonym expansion fired on every applicable line** (visible
  in the normalized queries): `green lumber → pressure treated`,
  `ledger → ledger board treated lumber`, `stringers → lumber stair stringer`,
  `LVL → laminated veneer lumber`, `H2.5 → Simpson H2.5 hurricane tie`,
  `thru locks → through bolt LedgerLOK`, `hidden fasteners → clip camo`,
  `LP → LP SmartSide Louisiana Pacific`, `spindles → baluster spindle`.
- **Dimension-triple rewrite** `2x10x12'` → `2x10 12ft` parsed size+length on
  every framing line.
- **Bonus stacking works**: `10' 6x6 posts treated` → `POST-6X6-PT` at 0.86;
  `12' 6x6 post treated` → `POST-6X6-PT-12` at 0.89 (size + length + treated,
  runner-up correctly the 10' variant); `2x4x12' blocks` → `RAIL-2X4-PT` 0.78.
- **Nearest-family fallback is sensible**: TimberTech/Deckorator/MoistureShield
  deck boards (absent from the demo catalog) land on the Trex composite lines
  of the right length at review-band confidence.

## Findings

### F1 — Document-context pollution creates a "black hole" item (HIGH)

`BALUSTER-WOOD` won **38 of 93 lines (41%)**. Root cause:
`enrich_description_for_matching` appends the full `global_material_context`
**and `job_notes`** to *every* line, and abbreviation expansion then amplifies
it. Evidence:

- Oakwood's context "Black handrail with wine cap, **round spindles**" expands
  to `baluster spindle` inside all 20 queries — dragging plywood, drip cap,
  and joist-hanger lines toward the baluster SKUs.
- Beisser's job note `2.5" color match screws?` made `SCREW-DECK-2.5B` the
  **runner-up on both deck-board lines** — a note about one line contaminated
  the others.
- Oakwood's job note "rail to house connection kit crossed out — do not
  order" is embedded verbatim in every query.

**Recommendation:** stop injecting `job_notes` into match text entirely; damp
`global_material_context` (e.g. only inject tokens that survive a stopword +
category filter, or weight context tokens separately in the vector query
instead of concatenating).

### F2 — Species penalty can outrank size correctness (MEDIUM)

`2x10x12' joists` (deck context ⇒ query contains "pressure treated") matched
`POST-6X6-PT-12` (0.52) over `JOIST-2X10-SPF`. The SPF joist takes the
species-conflict penalty (−0.12) which exceeds its size advantage (+0.08 size
bonus vs −0.08 dimension mismatch on the post = 0.16 spread but split across
different signals; the treated post also got the length bonus). Wrong size
should never beat right size for dimensional lumber.

**Recommendation:** either scale `_SPECIES_CONFLICT_PENALTY` down when the
explicit NxN size matches, or raise `_dimension_mismatch_penalty` above the
species penalty. Also: species signals injected from *context keywords*
("treated exterior pressure treated") should be weaker than species written
on the line itself.

### F3 — No "no-match" floor (MEDIUM)

Every line returns a named SKU, even at 0.27 confidence ("case caulk white" →
`BALUSTER-ALUM`). The review UI compensates, but auto-flows (CSV export,
future auto-confirm) would ship garbage.

**Recommendation:** below a floor (~0.45 on this catalog), return
`matched_item_code = None` and keep candidates for the picker.

### F4 — `_COLOR_TO_BRAND` gaps and collisions (LOW)

- `Cape Cod Gray` (MoistureShield Vantage) is missing from the table.
- `Sedona` maps to **azek**, but this job was *Deckorator Voyager* Sedona —
  brand implication would mis-signal on a full catalog carrying both.
- `Antique Leather` → timbertech resolved correctly for Espelund.

**Recommendation:** add MoistureShield colors (Cape Cod Gray, Spanish Leather,
Seasoned Mahogany, …) and make color→brand implication yield to an explicitly
parsed brand when the two disagree.

### F5 — Catalog gaps dominate the weak band (expected here)

I-joists, LVL, rim board, hurricane ties, Westbury railing parts, drip cap,
RainEscapes, LP siding, and all interior/trim/door items simply don't exist in
the 41-item demo catalog. This is the demo-catalog artifact, not a matcher
defect — but it confirms HANDOFF direction 3 (catalog-side enrichment) and
suggests re-running this harness against the production catalog on the Pi:

```bash
python scripts/match_real_docs.py --catalog <production_export.csv>
```

## Reproducing

```bash
# hybrid (downloads the model, or use --onnx-dir in locked-down envs)
python scripts/match_real_docs.py --json-out results.json

# CI-safe fuzzy-only regression
python -m unittest tests.test_real_doc_fixtures
```
