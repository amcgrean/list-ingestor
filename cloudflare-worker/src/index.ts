import Anthropic from "@anthropic-ai/sdk";
import { zodOutputFormat } from "@anthropic-ai/sdk/helpers/zod";
import { ResultSchema, type MatchResult } from "./schema";
import { CATALOG_SKUS, CATALOG_TEXT, SYSTEM_INSTRUCTIONS } from "./prompt";

interface Env {
  ASSETS: Fetcher;
  MODEL?: string;
  ANTHROPIC_API_KEY?: string;
  ACCESS_CODE?: string;
}

const IMAGE_TYPES = new Set(["image/jpeg", "image/png", "image/webp", "image/gif"]);
const PDF_TYPE = "application/pdf";
// Claude requests cap at 32MB; leave headroom for the catalog + prompt.
const MAX_TOTAL_BASE64 = 25 * 1024 * 1024;

interface UploadedFile {
  media_type: string;
  data: string; // base64, no data: prefix
}

interface ProcessRequest {
  files: UploadedFile[];
  notes?: string;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/api/health") {
      return json({
        ok: true,
        model: env.MODEL || "claude-opus-5",
        catalog_items: CATALOG_SKUS.size,
        server_key: Boolean(env.ANTHROPIC_API_KEY),
        access_code_required: Boolean(env.ACCESS_CODE),
      });
    }

    if (url.pathname === "/api/process") {
      if (request.method !== "POST") {
        return json({ error: "POST only" }, 405);
      }
      return handleProcess(request, env);
    }

    // Everything else falls through to static assets (the page).
    return env.ASSETS.fetch(request);
  },
};

async function handleProcess(request: Request, env: Env): Promise<Response> {
  if (env.ACCESS_CODE && request.headers.get("x-access-code") !== env.ACCESS_CODE) {
    return json({ error: "Wrong or missing access code." }, 403);
  }

  const apiKey = env.ANTHROPIC_API_KEY || request.headers.get("x-user-api-key") || "";
  if (!apiKey) {
    return json(
      { error: "No API key. Set the ANTHROPIC_API_KEY worker secret, or paste a key in Settings." },
      401
    );
  }

  let body: ProcessRequest;
  try {
    body = (await request.json()) as ProcessRequest;
  } catch {
    return json({ error: "Body must be JSON." }, 400);
  }

  const files = Array.isArray(body.files) ? body.files : [];
  if (files.length === 0) {
    return json({ error: "Add at least one photo or PDF." }, 400);
  }
  let total = 0;
  for (const f of files) {
    if (!f || typeof f.data !== "string" || typeof f.media_type !== "string") {
      return json({ error: "Each file needs media_type and base64 data." }, 400);
    }
    if (!IMAGE_TYPES.has(f.media_type) && f.media_type !== PDF_TYPE) {
      return json(
        { error: `Unsupported type ${f.media_type}. Use JPEG/PNG/WebP/GIF or PDF.` },
        400
      );
    }
    total += f.data.length;
  }
  if (total > MAX_TOTAL_BASE64) {
    return json({ error: "Upload too large — keep it under ~25MB total." }, 413);
  }

  const content: Anthropic.ContentBlockParam[] = files.map((f) =>
    f.media_type === PDF_TYPE
      ? {
          type: "document" as const,
          source: { type: "base64" as const, media_type: "application/pdf" as const, data: f.data },
        }
      : {
          type: "image" as const,
          source: {
            type: "base64" as const,
            media_type: f.media_type as "image/jpeg" | "image/png" | "image/webp" | "image/gif",
            data: f.data,
          },
        }
  );

  const notes = (body.notes || "").trim();
  content.push({
    type: "text",
    text:
      "Read this material list and match every line to the catalog." +
      (notes ? `\n\nContext from the person uploading: ${notes}` : ""),
  });

  const client = new Anthropic({ apiKey });
  const model = env.MODEL || "claude-opus-5";

  try {
    // Streaming keeps the request alive for large documents; the catalog block
    // carries cache_control so repeat runs reuse the cached prefix for 1h.
    const stream = client.messages.stream({
      model,
      max_tokens: 32000,
      system: [
        { type: "text", text: SYSTEM_INSTRUCTIONS },
        {
          type: "text",
          text: CATALOG_TEXT,
          cache_control: { type: "ephemeral", ttl: "1h" },
        },
      ],
      messages: [{ role: "user", content }],
      output_config: { format: zodOutputFormat(ResultSchema) },
    });
    const response = await stream.finalMessage();

    if (response.stop_reason === "refusal") {
      return json(
        {
          error:
            "Claude declined this request" +
            (response.stop_details?.explanation ? `: ${response.stop_details.explanation}` : "."),
        },
        502
      );
    }
    if (response.stop_reason === "max_tokens") {
      return json(
        { error: "Document too long for one pass — split it into two uploads." },
        502
      );
    }

    const text = response.content
      .filter((b): b is Anthropic.TextBlock => b.type === "text")
      .map((b) => b.text)
      .join("");
    let parsed: MatchResult;
    try {
      parsed = ResultSchema.parse(JSON.parse(text));
    } catch {
      return json({ error: "Claude returned an unreadable result — try again." }, 502);
    }

    // Guard: only SKUs that actually exist in the catalog survive.
    for (const line of parsed.lines) {
      if (line.matched_sku && !CATALOG_SKUS.has(line.matched_sku)) {
        line.note = [line.note, `Claude suggested unknown sku "${line.matched_sku}" — cleared.`]
          .filter(Boolean)
          .join(" ");
        line.matched_sku = null;
        line.matched_description = null;
        line.confidence = 0;
      }
      line.alternates = line.alternates.filter((a) => CATALOG_SKUS.has(a.sku));
    }

    return json({
      result: parsed,
      usage: {
        model: response.model,
        input_tokens: response.usage.input_tokens,
        output_tokens: response.usage.output_tokens,
        cache_read_input_tokens: response.usage.cache_read_input_tokens ?? 0,
        cache_creation_input_tokens: response.usage.cache_creation_input_tokens ?? 0,
      },
    });
  } catch (error) {
    if (error instanceof Anthropic.AuthenticationError) {
      return json({ error: "Invalid Claude API key." }, 401);
    }
    if (error instanceof Anthropic.RateLimitError) {
      return json({ error: "Rate limited by the Claude API — wait a minute and retry." }, 429);
    }
    if (error instanceof Anthropic.BadRequestError) {
      return json({ error: `Claude rejected the request: ${error.message}` }, 400);
    }
    if (error instanceof Anthropic.APIError) {
      return json({ error: `Claude API error (${error.status}): ${error.message}` }, 502);
    }
    return json({ error: "Unexpected error processing the document." }, 500);
  }
}
