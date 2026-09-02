import { z } from "zod";

export const AlternateSchema = z.object({
  sku: z.string(),
  description: z.string(),
});

export const LineSchema = z.object({
  n: z.number().describe("1-based line number in reading order"),
  quantity: z.number().nullable().describe("Quantity as written; null if unreadable"),
  unit: z
    .string()
    .nullable()
    .describe("Unit if stated or implied: EA, LF, SF, BF, PC, BOX, LBS, ..."),
  raw_text: z.string().describe("The line as written on the document, verbatim"),
  interpreted: z
    .string()
    .describe("Your normalized reading of the line in plain trade terms"),
  matched_sku: z
    .string()
    .nullable()
    .describe("Exact sku from the catalog, or null when no catalog item fits"),
  matched_description: z
    .string()
    .nullable()
    .describe("The catalog description for matched_sku, copied exactly"),
  confidence: z
    .number()
    .describe("0-1 honest confidence that matched_sku is what the writer wants"),
  alternates: z
    .array(AlternateSchema)
    .describe("Up to 2 plausible runner-up catalog items, best first"),
  note: z
    .string()
    .nullable()
    .describe(
      "Anything the counter person should know: catalog gap, ambiguity, crossed-out line, size uncertainty"
    ),
});

export const ResultSchema = z.object({
  document_summary: z
    .string()
    .describe("One or two sentences: what this document is and what job it's for"),
  customer: z.string().nullable().describe("Customer/contact name if written"),
  project: z.string().nullable().describe("Job name or address if written"),
  lines: z.array(LineSchema),
});

export type MatchResult = z.infer<typeof ResultSchema>;
export type MatchLine = z.infer<typeof LineSchema>;
