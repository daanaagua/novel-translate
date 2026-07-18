import { createHash } from "node:crypto";

import type { StreamFn } from "@earendil-works/pi-agent-core";
import type { Model } from "@earendil-works/pi-ai";

import type { StableTerm, V4Block } from "../domain/types.js";
import type { BudgetLedger } from "../kernel/budget.js";
import { getSourceLanguageProfile } from "../language/profiles.js";
import type { SourceLanguageProfile } from "../language/types.js";
import { assertNotAborted, Type, type TypedToolSpec } from "../tools/tool-spec.js";
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
  sourceLanguageProfile?: SourceLanguageProfile;
  signal?: AbortSignal;
  deadlineMs?: number;
}

export interface LexicalAnchorOutcome {
  anchors: LexicalAnchor[];
  terms: StableTerm[];
  run: PiRunResult;
}

function establishedForms(stableTerms: readonly StableTerm[]): string[] {
  return stableTerms.flatMap((term) => [term.sourceForm, term.canonicalSource]);
}

export function collectRepeatedAnchorCandidates(
  blocks: readonly V4Block[],
  stableTerms: readonly StableTerm[],
  profile: SourceLanguageProfile = getSourceLanguageProfile("en"),
): AnchorCandidate[] {
  return profile.collectAnchorCandidates({
    targetTexts: blocks.map((block) => block.sourceText),
    corpusTexts: blocks.map((block) => block.sourceText),
    establishedSourceForms: establishedForms(stableTerms),
    limit: 24,
  }).filter((candidate) => candidate.corpusFrequency >= 2)
    .slice(0, 12)
    .map((candidate) => ({
      sourceForm: candidate.sourceForm,
      contexts: candidate.contexts,
    }));
}

/**
 * Finds forms in the current window and builds compact translator-global
 * concordance from the complete source using the selected language profile.
 */
export function collectWindowAnchorCandidates(
  targetBlocks: readonly V4Block[],
  corpusBlocks: readonly V4Block[],
  stableTerms: readonly StableTerm[],
  decidedSourceForms: readonly string[] = [],
  profile: SourceLanguageProfile = getSourceLanguageProfile("en"),
): AnchorCandidate[] {
  return profile.collectAnchorCandidates({
    targetTexts: targetBlocks.map((block) => block.sourceText),
    corpusTexts: corpusBlocks.map((block) => block.sourceText),
    establishedSourceForms: [
      ...establishedForms(stableTerms),
      ...decidedSourceForms,
    ],
    limit: 12,
  }).map((candidate) => ({
    sourceForm: candidate.sourceForm,
    contexts: candidate.contexts,
  }));
}

export class LexicalAnchorer {
  constructor(private readonly runtime: PiRuntime) {}

  async run(input: LexicalAnchorInput): Promise<LexicalAnchorOutcome> {
    const profile = input.sourceLanguageProfile ?? getSourceLanguageProfile("en");
    const allowed = new Map(input.candidates.map((candidate) => [
      profile.normalizeSourceForm(candidate.sourceForm),
      candidate.sourceForm,
    ]));
    let anchors: LexicalAnchor[] = [];
    const tool: TypedToolSpec = {
      name: "submit_lexical_anchors",
      label: "Submit lexical anchors",
      description: "Classify every supplied source-language form and bind only context-invariant forms to one Chinese target.",
      phase: "translation",
      parameters: Type.Object({
        anchors: Type.Array(Type.Object({
          sourceForm: Type.String(),
          target: Type.String(),
          mode: Type.Union([Type.Literal("stable"), Type.Literal("contextual")]),
          confidence: Type.Number({ minimum: 0, maximum: 1 }),
        }), { maxItems: 24 }),
      }),
      execute: async (rawArgs, signal) => {
        assertNotAborted(signal);
        const args = rawArgs as { anchors: LexicalAnchor[] };
        if (!Array.isArray(args.anchors) || args.anchors.length !== allowed.size) {
          throw new Error(`expected exactly ${allowed.size} lexical anchor decisions`);
        }
        const seen = new Set<string>();
        for (const anchor of args.anchors) {
          const key = profile.normalizeSourceForm(anchor.sourceForm);
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
        `The source language is ${profile.displayName} (${profile.id}).`,
        "Mark proper names, unique titles, and invariant technical terms as stable and choose one concise Chinese target.",
        "Mark ordinary words, forms whose Chinese rendering changes by discourse role, and forms of address as contextual.",
        "Do not force surface consistency where Chinese grammar or relationship context requires variation.",
        "Call submit_lexical_anchors exactly once and classify every supplied form.",
      ].join("\n"),
      prompt: [
        "SOURCE-LANGUAGE FORMS AND COMPACT CONCORDANCE",
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
