# List Matcher — Cloudflare Worker

A single-Worker version of the material list ingestor: a page where you
photograph a contractor's handwritten list, and Claude reads it and matches
every line against a flat file of stocked SKUs — no database, no OCR stack,
no embedding models.

## How it works

- `public/index.html` — the page. Add photos (or a PDF), optional job context,
  hit **Match against catalog**. Results come back as an editable review table
  (swap in alternates, fix quantities, delete lines) with CSV export.
- `src/index.ts` — `POST /api/process`. Sends one Claude Messages API request:
  system prompt (trade-language matching rules) + the full catalog (with
  `cache_control`, so repeat runs within an hour reuse the cached catalog at
  ~10% input cost) + the uploaded images/PDF. Structured output is enforced
  with a JSON schema; returned SKUs are validated against the catalog
  server-side so a hallucinated SKU can never reach the table.
- `src/catalog.json` — the flat SKU file (bundled into the worker, not
  publicly served). Regenerate it from any Agility stocked-items export:

  ```bash
  python scripts/build_catalog.py items_stocking_uom.xlsx --branch 10FD
  npx wrangler deploy
  ```

## Deploy

```bash
cd cloudflare-worker
npm install
npx wrangler deploy                       # first deploy prompts a Cloudflare login
npx wrangler secret put ANTHROPIC_API_KEY # paste a Claude API key
npx wrangler secret put ACCESS_CODE       # optional shared passcode for the page
```

If you skip the `ANTHROPIC_API_KEY` secret, the page's Settings panel accepts
a key per-browser (kept in localStorage, sent per request, never stored
server-side).

**Protecting the page:** the optional `ACCESS_CODE` is a light gate. For real
protection put the worker behind Cloudflare Access (Zero Trust → Access →
Applications), same as the Pi app.

## Model & cost

Default model is `claude-opus-5` (override with the `MODEL` var in
`wrangler.jsonc`). Ballpark per document: the catalog is ~45K input tokens —
about $0.25 on the first run of the hour, then ~$0.05–0.15 per run while the
cache is warm; the page shows exact token usage and estimated cost after each
run.

## Local dev

```bash
npm run dev      # wrangler dev (uses .dev.vars for ANTHROPIC_API_KEY)
npm run check    # tsc --noEmit + wrangler dry-run build
```

Create `cloudflare-worker/.dev.vars` (gitignored) with
`ANTHROPIC_API_KEY=sk-ant-...` for local runs.

## Notes / limits

- Accepted uploads: JPEG/PNG/WebP/GIF and PDF, ~25MB total per run. The page
  re-encodes photos to ≤2000px JPEG before upload (also converts HEIC where
  the browser can decode it — on other browsers, share iPhone photos as JPEG).
- One catalog per deployment. For multiple branches, deploy one worker per
  branch (`name` + catalog file per branch) or extend `/api/process` to take
  a branch parameter with multiple bundled catalogs.
- This is intentionally session-free: no feedback learning, no aliases, no
  history. The main Flask app remains the full-pipeline system; this worker is
  the lightweight field tool.
