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

---

# Round 2 — Real Beisser catalog (same day)

Re-ran the same six fixtures against a **real Agility stocked-items export**
(`items_stocking_uom.xlsx` from Drive: `item, description, ext_description,
size_, stocking_uom`, 1,546 rows → 1,544 usable). The export was converted
with the production raw-catalog path — `sku_pipeline.preprocess_raw_catalog`
with the absent raw columns blanked and `system_id=10FD` — so size/length/
color/keywords derivation is exactly what a live import produces. The catalog
CSV itself is **not** committed (business data); full per-line results are in
`docs/real_doc_match_results_beisser10FD.json`.

**Catalog scope matters:** this export is an interior millwork/doors/trim
branch catalog — 47 bifolds, MDF/oak/pine mouldings, birch plywood — with
essentially **no deck program** (0 hits for trex/timbertech/westbury/
hurricane/6x6/hardie/moistureshield, 1 treated lumber item). So the three
deck documents are out-of-domain here and mostly *should not* match; the
doors and built-ins documents are squarely in-domain.

## Headline numbers (real catalog)

| Document | strong ≥0.75 | review 0.55–0.75 | weak <0.55 | in domain? |
|---|---|---|---|---|
| steve_hick_doors (11) | 0 | 4 | 7 | **yes** |
| builtins_rooms (6) | 2 | 1 | 3 | **yes** |
| earl_trim_boards (8) | 0 | 2 | 6 | partly |
| espelund_deck (40) | 0 | 1 | 39 | no |
| oakwood_deck (20) | 0 | 5 | 15 | no |
| beisser_moistureshield (8) | 0 | 4 | 4 | no |

Nothing out-of-domain crossed 0.75 — an auto-confirm bar at 0.75 would have
shipped **zero** wrong lines. But the 0.55–0.75 review band is polluted by
the context black-hole below.

## Round-2 findings

### R1 — Size+length+species stacking works at real-catalog scale (WIN)

`1x6 primed pine @ 16ft` → `16s4sprime16` (1x6-16' S4S FJ primed pine) at
**0.87**, and `1x4 …` → `14s4sprime16` at **0.87** — exact right answers out
of 1,544 items. `sheets 1/4" plywood` (built-ins context) → `pineac4825`
(4x8-1/4" AC radiata pine plywood, 0.58) — a defensible pick with birch 1/4"
as runner-up. `case caulk white` → `caulkpolacrwht` (white acrylic caulk) is
**correct** but scores only 0.50 — right answers can sit below the review
band on a big catalog (floor calibration must be per-catalog, see F3).

### R2 — F1 context pollution is *worse* on a real catalog (HIGH, confirms F1)

All 20 Oakwood lines — plywood, drip cap, joist hangers, deck boards — match
`handrailbkmtbkhd` (**handrail bracket-HD-matte black**) with an identical
fuzzy score of 0.52. `token_set_ratio` scores the *context suffix* ("Black
handrail with wine cap…") as a full subset match against the bracket's short
text, and per-line signal drowns. Espelund: 8 of 40 lines land on
`slfgtside1p1068` (a fiberglass **sidelite**). With 1,544 items there is
always *some* item that resonates with the injected context, so every
document develops a black-hole SKU. This is the single highest-leverage fix.

### R3 — Door size-code vocabulary gap (NEW, HIGH for door branches)

Contractors write door sizes as `2-8`, `2-6`, `3-0`; the catalog writes
Agility 4-digit codes `2868`, `2667`/`2668`, `3068`. Nothing bridges them:

- `2-6 bifold door` → `slstl6p2868db` (2868 6-panel **steel** door), while
  `2667 1-3/8" hc primed flush bifold` sits unmatched in the catalog.
- All four `2-8 …` prehung lines → the same 2868 steel door (right width by
  luck of the vector, wrong product family; runner-up was a 3068).

**Recommendation:** normalize `W-H` size tokens in `normalise_description`:
`2-8` → inject `2868`, `2-6` → `2667 2668` (bifolds are 6'7"), `3-0` →
`3068`, etc. This is a mechanical `_ABBREVIATIONS`-style rewrite and would
fix the whole class.

### R4 — Trade profile names missing from the abbreviation table (NEW, MEDIUM)

- `nickel gap` → junk (`clayjamb491680`, 0.43) while
  `11/16x5-1/4"-16' mdf sl16 shiplap primed` exists. Add
  `nickel gap → shiplap nickel gap`.
- `base 4 1/2 MDF flat` → `pinebase3col00` (pine colonial 3") while
  `414 e1e 1/2"x4-1/4"-16' mdf base` exists; `casing 3 1/2 MDF flat` →
  oak colonial 2-1/4" while `412 e2e 19/32"x3-1/2"-16' mdf casing` exists.
  Two compounding causes: profile codes (`414`, `e1e`, `sl16`) carry no
  semantic signal, and **material mismatch (MDF vs pine/oak) is unpenalized**
  — the finish-conflict machinery covers metals only. Consider a wood/sheet
  material conflict group (mdf | pine | oak | poplar | birch) mirroring
  `_FINISH_CONFLICT_GROUPS`, and catalog-side keyword enrichment for profile
  numbers ("flat", face width in inches).
- `hook strip` → weak 1-1/2" pine match; add `hook strip → closet hook strip
  s4s pine` if that's the stocked SKU family.

### R5 — Deck docs against a millwork branch: correct rejection, noisy top hits

39 of 40 Espelund lines below 0.55 is the *right* outcome for a catalog with
no deck program — the confidence signal separates cleanly. But the named top
hits are semantically absurd (deck boards → sidelite), which reinforces F3:
below a floor, return no match instead of the least-bad SKU. It also
confirms branch-scoped matching (`cache_key=branch:N`) is load-bearing:
these documents need to be tested against a deck-stocking branch export
(the Pi master catalog with treated/Trex/Westbury per HANDOFF session 25).

## Reproducing round 2

```bash
# 1. Convert the Agility export (fills absent raw columns, system_id=10FD)
python - <<'PY'
import pandas as pd
from app.services.sku_pipeline import preprocess_raw_catalog
src = pd.read_excel("items_stocking_uom.xlsx")
for col in ["major_description","minor_description","keyword_string",
            "keyword_user_defined","last_sold_date"]:
    src[col] = ""
src["system_id"] = "10FD"
out = preprocess_raw_catalog(src)
out["item_code"] = out["sku"]
out.to_csv("beisser_catalog_10FD.csv", index=False)
PY

# 2. Match
python scripts/match_real_docs.py --catalog beisser_catalog_10FD.csv \
    --json-out results_real.json
```
