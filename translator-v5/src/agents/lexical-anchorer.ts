import { createHash } from "node:crypto";

import type { StreamFn } from "@earendil-works/pi-agent-core";
import type { Model } from "@earendil-works/pi-ai";

import type { StableTerm, V4Block } from "../domain/types.js";
import type { BudgetLedger } from "../kernel/budget.js";
import {
  assertNotAborted,
  Type,
  type TypedToolSpec,
} from "../tools/tool-spec.js";
import { PiRuntime, type PiRunResult } from "./pi-runtime.js";

export interface AnchorCandidate {
  sourceForm: string;
  contexts: string[];
}

export interface LexicalAnchor {
  sourceForm: string;
  target: string;
  mode: "stable" | "contextual";
  confidence: number;
}

interface LexicalAnchorInput {
  candidates: readonly AnchorCandidate[];
  stableTerms: readonly StableTerm[];
  model: Model<any>;
  streamFn: StreamFn;
  budget: BudgetLedger;
  signal?: AbortSignal;
  deadlineMs?: number;
}

export interface LexicalAnchorOutcome {
  anchors: LexicalAnchor[];
  terms: StableTerm[];
  run: PiRunResult;
}

const COMMON_CAPITALIZED = new Set([
  "after", "again", "all", "although", "and", "another", "any", "are",
  "as", "at", "because", "before", "but", "by", "can", "could", "did",
  "do", "even", "for", "from", "had", "has", "have", "he", "her", "here",
  "far", "his", "how", "i", "if", "i’m", "in", "instead", "is", "it", "its",
  "later", "look", "may", "more", "my", "new", "no", "not", "now", "of", "on",
  "once", "one", "only", "or", "our", "perhaps", "she", "so", "some", "such",
  "still", "sun", "than", "that", "the", "their", "then",
  "there", "these", "they", "this", "those", "though", "to", "very", "was",
  "we", "were", "what", "when", "where", "which", "while", "who", "why",
  "will", "with", "would", "yes", "yet", "you", "your",
]);

function compactContext(text: string, offset: number): string {
  const left = Math.max(
    text.lastIndexOf(".", offset - 1),
    text.lastIndexOf("?", offset - 1),
    text.lastIndexOf("!", offset - 1),
    text.lastIndexOf("\n", offset - 1),
  );
  const endings = [
    text.indexOf(".", offset),
    text.indexOf("?", offset),
    text.indexOf("!", offset),
    text.indexOf("\n", offset),
  ].filter((value) => value >= 0);
  const right = endings.length === 0 ? text.length : Math.min(...endings) + 1;
  return text.slice(left + 1, right).replace(/\s+/gu, " ").trim().slice(0, 360);
}

export function collectRepeatedAnchorCandidates(
  blocks: readonly V4Block[],
  stableTerms: readonly StableTerm[],
): AnchorCandidate[] {
  const established = new Set(stableTerms.flatMap((term) => [
    term.sourceForm.toLocaleLowerCase(),
    term.canonicalSource.toLocaleLowerCase(),
    ...term.sourceForm.split(/[^A-Za-z'’-]+/u)
      .filter((part) => part.length >= 3)
      .map((part) => part.toLocaleLowerCase()),
    ...term.canonicalSource.split(/[^A-Za-z'’-]+/u)
      .filter((part) => part.length >= 3)
      .map((part) => part.toLocaleLowerCase()),
  ]));
  const found = new Map<string, { sourceForm: string; contexts: Set<string>; count: number }>();
  for (const block of blocks) {
    for (const match of block.sourceText.matchAll(/\b[A-Z][A-Za-z'’-]{2,}\b/gu)) {
      const sourceForm = match[0];
      const key = sourceForm.replace(/[’']s$/u, "").toLocaleLowerCase();
      if (COMMON_CAPITALIZED.has(key) || established.has(key)) {
        continue;
      }
      const record = found.get(key) ?? {
        sourceForm: sourceForm.replace(/[’']s$/u, ""),
        contexts: new Set<string>(),
        count: 0,
      };
      record.count += 1;
      const context = compactContext(block.sourceText, match.index);
      if (context.length > 0 && record.contexts.size < 3) {
        record.contexts.add(context);
      }
      found.set(key, record);
    }
  }
  return [...found.values()]
    .filter((record) => record.count >= 2)
    .sort((left, right) => right.count - left.count
      || left.sourceForm.localeCompare(right.sourceForm))
    .slice(0, 12)
    .map((record) => ({
      sourceForm: record.sourceForm,
      contexts: [...record.contexts],
    }));
}

/**
 * Finds forms that occur in the current window, but builds their compact
 * translator-global concordance from the whole source corpus. A prior stable
 * or contextual decision suppresses reconsideration.
 */
export function collectWindowAnchorCandidates(
  targetBlocks: readonly V4Block[],
  corpusBlocks: readonly V4Block[],
  stableTerms: readonly StableTerm[],
  decidedSourceForms: readonly string[] = [],
): AnchorCandidate[] {
  const established = new Set([
    ...stableTerms.flatMap((term) => [term.sourceForm, term.canonicalSource]),
    ...decidedSourceForms,
  ].map((form) => form.replace(/[’']s$/u, "").toLocaleLowerCase()));
  const targets = new Map<string, string>();
  for (const block of targetBlocks) {
    for (const match of block.sourceText.matchAll(/\b[A-Z][A-Za-z'’-]{2,}\b/gu)) {
      const sourceForm = match[0].replace(/[’']s$/u, "");
      const key = sourceForm.toLocaleLowerCase();
      if (!COMMON_CAPITALIZED.has(key) && !established.has(key)) {
        targets.set(key, sourceForm);
      }
    }
  }
  const found = new Map<string, { count: number; contexts: Set<string> }>();
  for (const block of corpusBlocks) {
    for (const match of block.sourceText.matchAll(/\b[A-Z][A-Za-z'’-]{2,}\b/gu)) {
      const key = match[0].replace(/[’']s$/u, "").toLocaleLowerCase();
      if (!targets.has(key)) {
        continue;
      }
      const record = found.get(key) ?? { count: 0, contexts: new Set<string>() };
      record.count += 1;
      if (record.contexts.size < 3) {
        const context = compactContext(block.sourceText, match.index);
        if (context.length > 0) {
          record.contexts.add(context);
        }
      }
      found.set(key, record);
    }
  }
  return [...found.entries()]
    .filter(([, record]) => record.count >= 2)
    .sort((left, right) =>
      right[1].count - left[1].count
      || (targets.get(left[0]) as string).localeCompare(targets.get(right[0]) as string))
    .slice(0, 12)
    .map(([key, record]) => ({
      sourceForm: targets.get(key) as string,
      contexts: [...record.contexts],
    }));
}

export class LexicalAnchorer {
  constructor(private readonly runtime: PiRuntime) {}

  async run(input: LexicalAnchorInput): Promise<LexicalAnchorOutcome> {
    const allowed = new Map(input.candidates.map((candidate) => [
      candidate.sourceForm.toLocaleLowerCase(),
      candidate.sourceForm,
    ]));
    let anchors: LexicalAnchor[] = [];
    const tool: TypedToolSpec = {
      name: "submit_lexical_anchors",
      label: "Submit lexical anchors",
      description: "Classify every repeated source form and bind only context-invariant forms to one Chinese target.",
      phase: "translation",
      parameters: Type.Object({
        anchors: Type.Array(Type.Object({
          sourceForm: Type.String(),
          target: Type.String(),
          mode: Type.Union([Type.Literal("stable"), Type.Literal("contextual")]),
          confidence: Type.Number({ minimum: 0, maximum: 1 }),
        }), { maxItems: 12 }),
      }),
      execute: async (rawArgs, signal) => {
        assertNotAborted(signal);
        const args = rawArgs as { anchors: LexicalAnchor[] };
        if (!Array.isArray(args.anchors) || args.anchors.length !== allowed.size) {
          throw new Error(`expected exactly ${allowed.size} lexical anchor decisions`);
        }
        const seen = new Set<string>();
        for (const anchor of args.anchors) {
          const key = anchor.sourceForm.toLocaleLowerCase();
          if (!allowed.has(key) || seen.has(key)) {
            throw new Error(`unknown or duplicate anchor form: ${anchor.sourceForm}`);
          }
          if (anchor.mode === "stable" && anchor.target.trim().length === 0) {
            throw new Error(`stable anchor requires a Chinese target: ${anchor.sourceForm}`);
          }
          if (!Number.isFinite(anchor.confidence)
            || anchor.confidence < 0 || anchor.confidence > 1) {
            throw new Error(`invalid anchor confidence: ${anchor.sourceForm}`);
          }
          seen.add(key);
        }
        input.budget.consume("translationToolCalls", 1);
        anchors = args.anchors.map((anchor) => ({ ...anchor }));
        return { accepted: true, anchors: anchors.length };
      },
    };
    const run = await this.runtime.run({
      systemPrompt: [
        "You establish run-local lexical anchors before parallel literary translation.",
        "Mark proper names, unique titles, and invariant technical terms as stable and choose one concise Chinese target.",
        "Mark ordinary words, forms whose Chinese rendering changes by discourse role, and address forms such as Archon/阁下 as contextual.",
        "Do not force surface consistency where Chinese grammar or relationship context requires variation.",
        "Call submit_lexical_anchors exactly once and classify every supplied form.",
      ].join("\n"),
      prompt: [
        "REPEATED FORMS AND LOCAL CONCORDANCE",
        JSON.stringify(input.candidates),
        "ESTABLISHED TERMS",
        input.stableTerms.map((term) =>
          `${term.sourceForm} => ${term.target}`).join("\n") || "(none)",
      ].join("\n\n"),
      phase: "translation",
      model: input.model,
      tools: [tool],
      budget: input.budget,
      terminateTools: ["submit_lexical_anchors"],
      maxTurns: 2,
      signal: input.signal,
      deadlineMs: input.deadlineMs,
    }, input.streamFn);
    return {
      anchors: anchors.map((anchor) => ({ ...anchor })),
      terms: anchors
        .filter((anchor) =>
          anchor.mode === "stable"
          && anchor.confidence >= 0.85
          && anchor.target.trim().length > 0)
        .map(anchorAsTerm),
      run,
    };
  }
}

export function anchorAsTerm(anchor: LexicalAnchor): StableTerm {
  const id = createHash("sha256")
    .update(`${anchor.sourceForm}\0${anchor.target}`)
    .digest("hex")
    .slice(0, 16);
  return {
    conceptId: `run-anchor-${id}`,
    lexemeId: `run-anchor-lexeme-${id}`,
    sourceForm: anchor.sourceForm,
    canonicalSource: anchor.sourceForm,
    target: anchor.target,
    locked: true,
  };
}
