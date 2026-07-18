import type { StreamFn } from "@earendil-works/pi-agent-core";
import type { Model } from "@earendil-works/pi-ai";

import type { StableTerm } from "../domain/types.js";
import type { PhysicalRequestPlan } from "../fullbook/types.js";
import type { BudgetLedger } from "../kernel/budget.js";
import { canonicalJson } from "../knowledge/knowledge-store.js";
import { getSourceLanguageProfile } from "../language/profiles.js";
import type { SourceLanguageProfile } from "../language/types.js";
import type { LosslessBlock } from "../source/types.js";
import type { TranslationMemoryCandidate } from "../tools/candidate-collector.js";
import {
  assertNotAborted,
  Type,
  type TypedToolSpec,
} from "../tools/tool-spec.js";
import { PiRuntime, type PiRunResult } from "./pi-runtime.js";

export interface FinalizeTranslationBatchArgs {
  windows: Array<{
    windowId: string;
    translations: Array<{ blockId: string; text: string }>;
    notes: string[];
    memoryCandidates?: TranslationMemoryCandidate[];
  }>;
}

export interface TranslationBatchSnapshot {
  readonly id: string;
  readonly revisions: readonly unknown[];
}

export interface TranslationBatchInput {
  request: PhysicalRequestPlan;
  blocks: readonly LosslessBlock[];
  stableTerms: readonly StableTerm[];
  snapshot: TranslationBatchSnapshot;
  model: Model<any>;
  streamFn: StreamFn;
  budget: BudgetLedger;
  styleState?: Readonly<Record<string, string>>;
  previousActiveTail?: string;
  sourceLanguageProfile?: SourceLanguageProfile;
  entityLinkWarnings?: readonly string[];
  signal?: AbortSignal;
  deadlineMs?: number;
}

export interface TranslationBatchWindowResult {
  windowId: string;
  ordinal: number;
  status: "completed" | "completed_with_warnings" | "failed";
  translations: Array<{ blockId: string; text: string }>;
  notes: string[];
  memoryCandidates: TranslationMemoryCandidate[];
  error?: string;
}

export interface TranslationBatchResult {
  requestId: string;
  snapshotId: string;
  windows: TranslationBatchWindowResult[];
  responseErrors: string[];
  run: PiRunResult;
}

function nonempty(value: string, label: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new TypeError(`${label} must be nonempty`);
  }
  return value;
}

function copyMemories(
  values: readonly TranslationMemoryCandidate[] | undefined,
): TranslationMemoryCandidate[] {
  return (values ?? []).map((candidate) => ({
    ...candidate,
    subjectForms: [...candidate.subjectForms],
  }));
}

function validateSubmission(
  input: TranslationBatchInput,
  submission: FinalizeTranslationBatchArgs | undefined,
): Pick<TranslationBatchResult, "windows" | "responseErrors"> {
  const expected = new Map(input.request.windows.map((window) => [window.windowId, window]));
  const blockById = new Map(input.blocks.map((block) => [block.id, block]));
  const entries = submission?.windows ?? [];
  const byWindow = new Map<string, FinalizeTranslationBatchArgs["windows"]>();
  const responseErrors: string[] = [];
  for (const entry of entries) {
    if (!expected.has(entry.windowId)) {
      responseErrors.push(`unknown windowId: ${entry.windowId}`);
      continue;
    }
    const grouped = byWindow.get(entry.windowId) ?? [];
    grouped.push(entry);
    byWindow.set(entry.windowId, grouped);
  }

  const windows = input.request.windows.map((window): TranslationBatchWindowResult => {
    const submitted = byWindow.get(window.windowId) ?? [];
    const failed = (error: string): TranslationBatchWindowResult => ({
      windowId: window.windowId,
      ordinal: window.ordinal,
      status: "failed",
      translations: [],
      notes: [],
      memoryCandidates: [],
      error,
    });
    if (submitted.length === 0) {
      return failed(`missing window submission: ${window.windowId}`);
    }
    if (submitted.length > 1) {
      return failed(`duplicate windowId: ${window.windowId}`);
    }
    const candidate = submitted[0] as FinalizeTranslationBatchArgs["windows"][number];
    const expectedIds = new Set(window.blockIds);
    const seen = new Set<string>();
    for (const translation of candidate.translations) {
      if (!expectedIds.has(translation.blockId) || !blockById.has(translation.blockId)) {
        return failed(`unknown blockId for ${window.windowId}: ${translation.blockId}`);
      }
      if (seen.has(translation.blockId)) {
        return failed(`duplicate blockId for ${window.windowId}: ${translation.blockId}`);
      }
      seen.add(translation.blockId);
      if (translation.text.trim().length === 0) {
        return failed(`empty translation for block ${translation.blockId}`);
      }
    }
    const missing = window.blockIds.filter((blockId) => !seen.has(blockId));
    if (missing.length > 0 || candidate.translations.length !== window.blockIds.length) {
      return failed(`block set mismatch for ${window.windowId}: missing ${missing.join(", ")}`);
    }
    return {
      windowId: window.windowId,
      ordinal: window.ordinal,
      status: candidate.notes.length > 0 ? "completed_with_warnings" : "completed",
      translations: candidate.translations.map((translation) => ({ ...translation })),
      notes: [...candidate.notes],
      memoryCandidates: copyMemories(candidate.memoryCandidates),
    };
  });
  return { windows, responseErrors };
}

function promptFor(input: TranslationBatchInput): string {
  const profile = input.sourceLanguageProfile ?? getSourceLanguageProfile("en");
  const blockById = new Map(input.blocks.map((block) => [block.id, block]));
  const windows = input.request.windows.map((window) => ({
    windowId: window.windowId,
    ordinal: window.ordinal,
    blocks: window.blockIds.map((blockId) => {
      const block = blockById.get(blockId);
      if (block === undefined) {
        throw new Error(`physical request references unknown block: ${blockId}`);
      }
      return { blockId, sourceText: block.sourceText };
    }),
  }));
  return [
    `PHYSICAL REQUEST ${input.request.requestId}`,
    `KNOWLEDGE SNAPSHOT ${input.snapshot.id}`,
    `SOURCE LANGUAGE ${profile.displayName} (${profile.id}); TARGET LANGUAGE Chinese (zh)`,
    "KNOWLEDGE SNAPSHOT REVISIONS",
    canonicalJson(input.snapshot.revisions),
    "WINDOWS",
    JSON.stringify(windows),
    "STABLE TERMS",
    JSON.stringify(input.stableTerms),
    "UNRESOLVED ENTITY LINKS",
    JSON.stringify(input.entityLinkWarnings ?? []),
    "PREVIOUS ACTIVE TAIL",
    input.previousActiveTail ?? "",
    "STYLE STATE",
    JSON.stringify(input.styleState ?? {}),
    "Translate every source block. Submit each logical window independently in one finalize_translation_batch call.",
  ].join("\n\n");
}

export async function runTranslationBatch(
  input: TranslationBatchInput,
): Promise<TranslationBatchResult> {
  nonempty(input.request.requestId, "requestId");
  nonempty(input.snapshot.id, "snapshotId");
  const profile = input.sourceLanguageProfile ?? getSourceLanguageProfile("en");
  let submission: FinalizeTranslationBatchArgs | undefined;
  let duplicateTerminalCalls = 0;
  const finalizeTool: TypedToolSpec = {
    name: "finalize_translation_batch",
    label: "Finalize translation batch",
    description: "Submit one complete response grouped by immutable logical window identity.",
    phase: "translation",
    parameters: Type.Object({
      windows: Type.Array(Type.Object({
        windowId: Type.String(),
        translations: Type.Array(Type.Object({
          blockId: Type.String(),
          text: Type.String(),
        }, { additionalProperties: false })),
        notes: Type.Array(Type.String()),
        memoryCandidates: Type.Optional(Type.Array(Type.Object({
          kind: Type.String(),
          subjectForms: Type.Array(Type.String(), { minItems: 1, maxItems: 3 }),
          fact: Type.String(),
          confidence: Type.Number({ minimum: 0, maximum: 1 }),
        }, { additionalProperties: false }), { maxItems: 4 })),
      }, { additionalProperties: false })),
    }, { additionalProperties: false }),
    execute: async (rawArgs, signal) => {
      assertNotAborted(signal);
      if (submission !== undefined) {
        duplicateTerminalCalls += 1;
        return { accepted: true };
      }
      input.budget.consume("translationToolCalls", 1);
      const args = rawArgs as FinalizeTranslationBatchArgs;
      submission = structuredClone(args);
      return { accepted: true };
    },
  };
  const run = await new PiRuntime().run({
    systemPrompt: [
      "Translate the complete source text into polished, accurate Chinese literary prose.",
      `The source language is ${profile.displayName} (${profile.id}); the target language is Chinese (zh).`,
      "Preserve meaning, ambiguity, paragraph structure, voice, and every block boundary.",
      "Logical windows remain independent even though this is one physical request.",
      "Use typed tools only and call finalize_translation_batch exactly once.",
    ].join("\n"),
    prompt: promptFor(input),
    phase: "translation",
    model: input.model,
    tools: [finalizeTool],
    budget: input.budget,
    terminateTools: ["finalize_translation_batch"],
    signal: input.signal,
    deadlineMs: input.deadlineMs,
  }, input.streamFn);
  const validated = validateSubmission(input, submission);
  if (duplicateTerminalCalls > 0) {
    validated.responseErrors.push(
      `multiple terminating submissions rejected: ${duplicateTerminalCalls + 1}`,
    );
  }
  return {
    requestId: input.request.requestId,
    snapshotId: input.snapshot.id,
    ...validated,
    run,
  };
}
