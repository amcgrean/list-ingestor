# On-Device Vision Benchmark

Measures whether an on-device extractor (Apple Vision / Android ML Kit) can
reproduce what the cloud multimodal Stage A pass produces today.

This is the decision-quality data for "can we drop the OpenAI dependency and
run extraction on the phone?" — it replaces guessing with numbers from your
own material lists.

---

## What is being measured

Stage A (`app/services/vision_extract_service.py`) does two jobs at once:

1. **Reads glyphs** — turns handwriting and print into text and quantities.
2. **Understands layout** — decides that `56 Grooved x 12` belongs under the
   heading `Cinnamon Cove`, and populates `section_header` on every child row.

Job 1 is what OCR engines do, and Apple's is good at it. Job 2 is what the
cloud model gives you by *seeing* the page, and it is the capability at risk
when moving on-device. The benchmark scores them separately so you can tell
which one fails.

| Metric | Question it answers |
|---|---|
| `line_recall` / `line_precision` | Did we find the same rows at all? |
| `mean_text_similarity` | Did we read the characters correctly? |
| `exact_text_rate` | How often was the read character-perfect? |
| `quantity_accuracy` | Did we read the counts correctly? |
| `header_attr_accuracy` | **Did rows get attributed to the right heading?** |
| `header_line_recall` | Were the heading rows themselves detected? |

`header_attr_accuracy` is the headline number. Text similarity above ~0.95
with header attribution near 0 means Apple Vision can read your lists but
cannot structure them — you would keep the cloud pass for Stage A. Both high
means on-device extraction is genuinely viable.

---

## Prerequisites — read this first

Uploads are written to a tempfile and **deleted after processing**
(`app/routes.py`, the `finally` block in `_process_session_background`).
Archived sessions keep their parsed output but not the source image, so
**sessions processed before you enable archiving cannot be re-run on-device.**

Turn both of these on and let a corpus accumulate:

```bash
PARSE_ARCHIVE_UPLOADS=true      # retain source images (default: false)
PARSE_DEBUG_SAVE_JSON=true      # full-fidelity Stage A ground truth (default: false)
```

`PARSE_ARCHIVE_UPLOADS` retains customer documents on disk indefinitely under
`PARSE_ARCHIVE_DIR` (default `data/upload_archive/`). Enable it deliberately,
and clean the directory out when the benchmark is done.

Without `PARSE_DEBUG_SAVE_JSON` the exporter falls back to reconstructing
ground truth from `extracted_items` rows. That works for text, quantity, and
header-attribution scoring, but Stage A header *lines* are not persisted as
items, so `header_line_recall` is unmeasurable for those sessions. The
exporter records which source it used in `expected.json` and the scorer warns
about it.

**Sample size**: 20–30 lists spanning handwritten, typed, and mixed formats.
Include the ugly ones — faint pencil, tally marks, crossed-out rows, side
annotations. A clean-typed-only corpus will tell you on-device works and then
fall over in the field.

---

## Workflow

### 1. Export the ground-truth corpus

```bash
python scripts/export_vision_benchmark.py --out data/vision_benchmark --require-images
```

Produces:

```
data/vision_benchmark/
  session_25/
    expected.json      <- cloud Stage A output
    01_list.jpg        <- archived source image
  session_31/
    expected.json
    01_page1.jpg
    02_page2.jpg
```

Useful flags: `--limit N` (newest N sessions), `--session-ids 25,31,44`,
`--require-images` (skip sessions with no retained source).

### 2. Run the on-device extractor

Copy the corpus to a Mac or iPhone and run each session's images through
Vision, writing `vision.json` next to `expected.json`. Reference
implementation below.

### 3. Score

```bash
python scripts/score_vision_benchmark.py --corpus data/vision_benchmark --detail
python scripts/score_vision_benchmark.py --corpus data/vision_benchmark --json report.json
```

Sessions with no `vision.json` are reported as pending and excluded from
totals, so you can score a partially-processed corpus.

Run the unit tests for the scorer with:

```bash
python -m unittest tests.test_vision_benchmark
```

---

## `vision.json` schema

Same line shape as `expected.json`, so scoring is symmetric.

```json
{
  "session_id": 25,
  "engine": "RecognizeDocumentsRequest",
  "lines": [
    {
      "line_id": "V1",
      "raw_text": "56 Grooved x 12",
      "section_header": "Cinnamon Cove",
      "section_type": "item",
      "quantity": 56
    }
  ]
}
```

| Field | Required | Notes |
|---|---|---|
| `raw_text` | yes | Text as read. Do not normalise or expand abbreviations — the scorer normalises both sides. |
| `quantity` | no | Omit or `null` if not parsed. Omitting counts as wrong when the ground truth has one. |
| `section_header` | no | The heading this row falls under. Empty string if none. **This is the metric that matters.** |
| `section_type` | no | `"header"` for heading rows, `"item"` otherwise. |
| `line_id` | no | Free-form; only used in reports. |

Emit lines in **document order** — the scorer aligns sequences with
Needleman-Wunsch and relies on order to avoid mispairing near-identical rows
(a deck list has a dozen similar joist lines).

Multi-page sessions: emit one `vision.json` covering all images, pages
concatenated in filename order, matching how Stage A treats a batch as one
document set.

---

## Swift reference implementation

Requires iOS 26 / macOS 26 for `RecognizeDocumentsRequest`. The
`VNRecognizeTextRequest` path works on older OSes but gives you observations
without document structure, so you would derive `section_header` purely from
`boundingBox` geometry.

```swift
import Foundation
import Vision

struct BenchmarkLine: Codable {
    var line_id: String
    var raw_text: String
    var section_header: String
    var section_type: String
    var quantity: Double?
}

struct BenchmarkDoc: Codable {
    var session_id: Int
    var engine: String
    var lines: [BenchmarkLine]
}

/// Leading count on a material row: "56 Grooved x 12" -> 56
private func leadingQuantity(_ text: String) -> Double? {
    let pattern = #"^\s*[-•*]?\s*(\d+(?:\.\d+)?)\b"#
    guard let match = text.range(of: pattern, options: .regularExpression) else { return nil }
    let digits = text[match].trimmingCharacters(in: CharacterSet(charactersIn: " -•*\t"))
    return Double(digits)
}

/// A heading is a short line with no leading count, often ending in a colon.
private func looksLikeHeader(_ text: String) -> Bool {
    let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !trimmed.isEmpty, trimmed.count <= 40 else { return false }
    if trimmed.hasSuffix(":") { return true }
    return leadingQuantity(trimmed) == nil && trimmed.split(separator: " ").count <= 4
}

func extract(imageURLs: [URL], sessionID: Int) async throws -> BenchmarkDoc {
    var lines: [BenchmarkLine] = []
    var currentHeader = ""
    var counter = 0

    for url in imageURLs {
        var request = RecognizeDocumentsRequest()
        request.recognitionLevel = .accurate
        // Domain vocabulary measurably helps on lumber shorthand.
        request.customWords = ["SYP", "SPF", "LVL", "OSB", "TJI", "HDG", "MCA",
                               "PT", "KD", "S4S", "WRC", "LUS", "Azek", "Trex"]

        let observations = try await request.perform(on: url)

        for document in observations {
            for paragraph in document.paragraphs {
                for line in paragraph.lines {
                    let text = line.transcript.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !text.isEmpty else { continue }
                    counter += 1

                    if looksLikeHeader(text) {
                        currentHeader = text.hasSuffix(":") ? String(text.dropLast()) : text
                        lines.append(BenchmarkLine(
                            line_id: "V\(counter)",
                            raw_text: currentHeader,
                            section_header: "",
                            section_type: "header",
                            quantity: 0
                        ))
                    } else {
                        lines.append(BenchmarkLine(
                            line_id: "V\(counter)",
                            raw_text: text,
                            section_header: currentHeader,
                            section_type: "item",
                            quantity: leadingQuantity(text)
                        ))
                    }
                }
            }
        }
    }

    return BenchmarkDoc(session_id: sessionID, engine: "RecognizeDocumentsRequest", lines: lines)
}
```

The `looksLikeHeader` heuristic is deliberately crude — it is the *baseline*.
Its accuracy against `header_attr_accuracy` tells you how much work real
layout reconstruction needs. Obvious next steps if the baseline scores poorly:
use `boundingBox.minX` to detect indentation, and font-size proxies via
bounding-box height to spot headings.

---

## Reading the results

| Outcome | Reading |
|---|---|
| Text sim > 0.95, header attr > 0.85 | On-device Stage A is viable. Pursue the full local pipeline. |
| Text sim > 0.95, header attr < 0.6 | Vision reads fine but can't structure. Keep cloud Stage A, or invest in geometry-based layout reconstruction and re-measure. |
| Text sim < 0.85 | On-device OCR isn't reading your lists. Check whether failures concentrate in handwritten samples before concluding. |
| Line recall < 0.8 | Rows are being dropped entirely — usually capture quality. Worth fixing regardless, since it also affects the cloud path. |

Segment by document type before drawing conclusions. Aggregate numbers hide
the split that matters: typed lists will score far higher than handwritten
ones, and your field uploads skew handwritten.

---

## What this does not measure

Only Stage A. Stage B (context interpretation, `context_interpreter.py`) is a
separate question — it is text-in/text-out and therefore a much easier fit for
an on-device model. If Stage A stays in the cloud, Stage B can still move
on-device independently, with catalog candidates retrieved locally and fed to
the model as context.
