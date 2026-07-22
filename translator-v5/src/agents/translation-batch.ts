import type { StreamFn, ThinkingLevel } from "@earendil-works/pi-agent-core";
import type { Model } from "@earendil-works/pi-ai";

import type { V4Block } from "../domain/types.js";
import type { BudgetLedger } from "../kernel/budget.js";
import type { LosslessBlock } from "../source/types.js";
import { normalizeChineseQuoteTexts } from "../style/chinese-quote-normalization.js";
import { simplifyChineseTranslation } from "../style/chinese-script-normalization.js";
import type { StyleObservationSubmission } from "../style/types.js";
import type {
  TranslationCandidate,
  TranslationMemoryCandidate,
} from "../tools/candidate-collector.js";
import type { ValidationFailure } from "../tools/repair-tools.js";
import { PiRuntime, type PiRunResult } from "./pi-runtime.js";
import { Repairer } from "./repairer.js";
import {
  prepareTranslationRequest,
  type FinalizeTranslationBatchArgs,
  type TranslationBatchSnapshot,
  type TranslationRequestInput,
} from "./translation-request.js";
import { TranslationValidator } from "../validators/translation-validator.js";
import { parseFramedTranslationResponse } from "./framed-translation-protocol.js";

export { translationBatchSystemPrompt } from "./translation-request.js";
export type {
  FinalizeTranslationBatchArgs,
  TranslationBatchSnapshot,
} from "./translation-request.js";

export interface TranslationBatchInput extends TranslationRequestInput {
  model: Model<any>;
  streamFn: StreamFn;
  thinkingLevel?: ThinkingLevel;
  repairRuntime?: {
    model: Model<any>;
    streamFn: StreamFn;
    thinkingLevel?: ThinkingLevel;
  };
  budget: BudgetLedger;
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
  styleObservation?: StyleObservationSubmission;
  error?: string;
}

export interface TranslationBatchResult {
  requestId: string;
  snapshotId: string;
  windows: TranslationBatchWindowResult[];
  responseErrors: string[];
  run: PiRunResult;
  repairRuns: PiRunResult[];
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

const CANONICAL_BLOCK_ID = /^block-[0-9a-f]{20}$/u;

interface BlockIdCorrection {
  blockId: string;
  hexOffset: number;
  submittedCharacter: string;
  canonicalCharacter: string;
}

function singleCharacterDifferenceIndex(left: string, right: string): number | undefined {
  if (left.length !== right.length) {
    return undefined;
  }
  let differenceIndex: number | undefined;
  for (let index = 0; index < left.length; index += 1) {
    if (left[index] !== right[index]) {
      if (differenceIndex !== undefined) {
        return undefined;
      }
      differenceIndex = index;
    }
  }
  return differenceIndex;
}

/**
 * Model output occasionally copies one hexadecimal character of an opaque
 * lossless block ID incorrectly.  This is deliberately a very narrow repair:
 * the submitted value itself must not name any real block, and exactly one
 * real ID in the whole input may be one character away. That target must be
 * canonical and expected by the current logical window.
 */
function correctUnknownCanonicalBlockId(
  submittedId: string,
  expectedIds: ReadonlySet<string>,
  realBlockIds: ReadonlySet<string>,
): BlockIdCorrection | undefined {
  if (!CANONICAL_BLOCK_ID.test(submittedId) || realBlockIds.has(submittedId)) {
    return undefined;
  }
  let candidate: { blockId: string; differenceIndex: number } | undefined;
  for (const realBlockId of realBlockIds) {
    const differenceIndex = singleCharacterDifferenceIndex(submittedId, realBlockId);
    if (differenceIndex === undefined) {
      continue;
    }
    if (candidate !== undefined) {
      return undefined;
    }
    candidate = { blockId: realBlockId, differenceIndex };
  }
  if (
    candidate === undefined
    || !CANONICAL_BLOCK_ID.test(candidate.blockId)
    || !expectedIds.has(candidate.blockId)
  ) {
    return undefined;
  }
  return {
    blockId: candidate.blockId,
    hexOffset: candidate.differenceIndex - "block-".length + 1,
    submittedCharacter: submittedId.charAt(candidate.differenceIndex),
    canonicalCharacter: candidate.blockId.charAt(candidate.differenceIndex),
  };
}

function correctedBlockIdWarning(windowId: string, correction: BlockIdCorrection): string {
  return "warning: mechanically corrected opaque blockId"
    + ` in window ${windowId} at hex offset ${correction.hexOffset}`
    + ` (${correction.submittedCharacter} -> ${correction.canonicalCharacter})`;
}

function validateSubmission(
  input: TranslationBatchInput,
  submission: FinalizeTranslationBatchArgs | undefined,
): Pick<TranslationBatchResult, "windows" | "responseErrors"> {
  const expected = new Map(input.request.windows.map((window) => [window.windowId, window]));
  const blockById = new Map(input.blocks.map((block) => [block.id, block]));
  const realBlockIds = new Set(blockById.keys());
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
    const translations: Array<{ blockId: string; text: string }> = [];
    const correctionWarnings: string[] = [];
    for (const translation of candidate.translations) {
      const correction = expectedIds.has(translation.blockId)
        ? undefined
        : correctUnknownCanonicalBlockId(
          translation.blockId,
          expectedIds,
          realBlockIds,
        );
      const blockId = correction?.blockId ?? translation.blockId;
      if (!expectedIds.has(blockId) || !blockById.has(blockId)) {
        return failed(`unknown blockId for ${window.windowId}: ${translation.blockId}`);
      }
      if (seen.has(blockId)) {
        return failed(`duplicate blockId for ${window.windowId}: ${translation.blockId}`);
      }
      seen.add(blockId);
      if (translation.text.trim().length === 0) {
        return failed(`empty translation for block ${blockId}`);
      }
      translations.push({ blockId, text: translation.text });
      if (correction !== undefined) {
        correctionWarnings.push(correctedBlockIdWarning(window.windowId, correction));
      }
    }
    const missing = window.blockIds.filter((blockId) => !seen.has(blockId));
    if (missing.length > 0 || candidate.translations.length !== window.blockIds.length) {
      return failed(`block set mismatch for ${window.windowId}: missing ${missing.join(", ")}`);
    }
    responseErrors.push(...correctionWarnings);
    return {
      windowId: window.windowId,
      ordinal: window.ordinal,
      status: candidate.notes.length > 0 ? "completed_with_warnings" : "completed",
      translations,
      notes: [...candidate.notes],
      memoryCandidates: copyMemories(candidate.memoryCandidates),
      ...(candidate.styleObservation === undefined
        ? {}
        : { styleObservation: structuredClone(candidate.styleObservation) }),
    };
  });
  return {
    windows: normalizeWindowTypography(
      windows,
      input.stableTerms.filter((term) => term.locked).map((term) => term.target),
    ),
    responseErrors,
  };
}

function losslessAsV4(block: LosslessBlock): V4Block {
  return {
    id: block.id,
    legacyId: null,
    chapterId: block.structureId,
    chapterTitle: block.structureTitle,
    globalIndex: block.globalIndex,
    blockIndex: block.globalIndex,
    sourceText: block.sourceText,
    sourceHash: block.sourceHash,
    tokenCount: block.tokenCount,
  };
}

function candidateFor(window: TranslationBatchWindowResult): TranslationCandidate {
  return {
    translations: window.translations.map((item) => ({ ...item })),
    notes: [...window.notes],
    memoryCandidates: copyMemories(window.memoryCandidates),
    repaired: false,
  };
}

function normalizeWindowTypography(
  windows: readonly TranslationBatchWindowResult[],
  preservedTargetForms: readonly string[],
): TranslationBatchWindowResult[] {
  let state = { openDoubleQuoteDepth: 0 };
  return windows.map((window) => {
    const normalized = normalizeChineseQuoteTexts(
      window.translations.map((translation) => translation.text),
      state,
    );
    state = normalized.state;
    return {
      ...window,
      translations: window.translations.map((translation, index) => ({
        ...translation,
        text: simplifyChineseTranslation(
          normalized.texts[index] ?? translation.text,
          preservedTargetForms,
        ),
      })),
    };
  });
}

function failureMessage(failures: readonly ValidationFailure[]): string {
  return failures.map((failure) => `${failure.code}: ${failure.message}`).join("; ");
}

function lastAssistantText(run: PiRunResult): string {
  const message = run.messages.findLast((item) =>
    "role" in item && item.role === "assistant");
  if (message === undefined || !("content" in message)) {
    return "";
  }
  if (typeof message.content === "string") {
    return message.content;
  }
  if (!Array.isArray(message.content)) {
    return "";
  }
  return message.content
    .filter((part): part is { type: "text"; text: string } =>
      typeof part === "object"
      && part !== null
      && "type" in part
      && part.type === "text"
      && "text" in part
      && typeof part.text === "string")
    .map((part) => part.text)
    .join("");
}

function framedSubmission(
  input: TranslationBatchInput,
  translations: readonly { blockId: string; text: string }[],
): FinalizeTranslationBatchArgs {
  const byId = new Map(translations.map((translation) => [translation.blockId, translation.text]));
  return {
    windows: input.request.windows.map((window) => ({
      windowId: window.windowId,
      translations: window.blockIds.flatMap((blockId) => {
        const text = byId.get(blockId);
        return text === undefined ? [] : [{ blockId, text }];
      }),
      notes: [],
    })),
  };
}

async function validateAndRepair(
  input: TranslationBatchInput,
  initial: Pick<TranslationBatchResult, "windows" | "responseErrors">,
): Promise<Pick<TranslationBatchResult, "windows" | "responseErrors" | "repairRuns">> {
  const validator = new TranslationValidator();
  const blockById = new Map(input.blocks.map((block) => [block.id, losslessAsV4(block)]));
  const validationPolicy = {
    allowedLatinTokens: input.stableTerms.flatMap((term) => [
      term.sourceForm,
      term.canonicalSource,
    ]),
    requiredTerms: input.stableTerms.filter((term) => term.locked)
      .map((term) => ({ sourceForm: term.sourceForm, target: term.target })),
    sourceLanguageProfile: input.sourceLanguageProfile,
  };
  const invalid = initial.windows.flatMap((window) => {
    if (window.status === "failed") {
      return [];
    }
    const blocks = input.request.windows.find((item) => item.windowId === window.windowId)
      ?.blockIds.map((blockId) => blockById.get(blockId))
      .filter((block): block is V4Block => block !== undefined) ?? [];
    const validation = validator.validate(blocks, candidateFor(window), validationPolicy);
    return validation.valid ? [] : [{ window, blocks, failures: validation.failures }];
  });
  if (invalid.length === 0) {
    return { ...initial, repairRuns: [] };
  }

  const repairBlockIds = new Set<string>();
  for (const item of invalid) {
    for (const failure of item.failures) {
      if (failure.blockId !== undefined) {
        repairBlockIds.add(failure.blockId);
      } else {
        item.blocks.forEach((block) => repairBlockIds.add(block.id));
      }
    }
  }
  const repairBlocks = [...repairBlockIds].map((blockId) => blockById.get(blockId))
    .filter((block): block is V4Block => block !== undefined)
    .sort((left, right) => left.globalIndex - right.globalIndex);
  const failedTranslations = invalid.flatMap((item) => item.window.translations)
    .filter((translation) => repairBlockIds.has(translation.blockId));
  const failures = invalid.flatMap((item) => item.failures);
  let repair: Awaited<ReturnType<Repairer["repairBatch"]>>;
  try {
    const repairRuntime = input.repairRuntime ?? {
      model: input.model,
      streamFn: input.streamFn,
      thinkingLevel: input.thinkingLevel,
    };
    repair = await new Repairer(new PiRuntime()).repairBatch({
      blocks: repairBlocks,
      failedCandidate: {
        translations: failedTranslations,
        notes: [],
        repaired: false,
      },
      failures,
      budget: input.budget,
      model: repairRuntime.model,
      streamFn: repairRuntime.streamFn,
      thinkingLevel: repairRuntime.thinkingLevel,
      signal: input.signal,
      deadlineMs: input.deadlineMs,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const invalidIds = new Set(invalid.map((item) => item.window.windowId));
    return {
      responseErrors: initial.responseErrors,
      repairRuns: [],
      windows: initial.windows.map((window) => invalidIds.has(window.windowId)
        ? {
          ...window,
          status: "failed" as const,
          translations: [],
          memoryCandidates: [],
          error: `targeted repair failed: ${message}`,
        }
        : window),
    };
  }
  const patchById = new Map(repair.candidate?.translations.map((item) => [
    item.blockId,
    item,
  ]) ?? []);
  const invalidById = new Map(invalid.map((item) => [item.window.windowId, item]));
  const patchedWindows = initial.windows.map((window): TranslationBatchWindowResult => {
    const item = invalidById.get(window.windowId);
    if (item === undefined) {
      return window;
    }
    const repairedWindow: TranslationBatchWindowResult = {
      ...window,
      translations: window.translations.map((translation) => ({
        ...(patchById.get(translation.blockId) ?? translation),
      })),
    };
    delete repairedWindow.styleObservation;
    return repairedWindow;
  });
  const windows = normalizeWindowTypography(
    patchedWindows,
    input.stableTerms.filter((term) => term.locked).map((term) => term.target),
  );
  const validatedWindows = windows.map((window): TranslationBatchWindowResult => {
    const item = invalidById.get(window.windowId);
    if (item === undefined || window.status === "failed") {
      return window;
    }
    const validation = validator.validate(
      item.blocks,
      candidateFor(window),
      validationPolicy,
    );
    if (validation.valid) {
      return window;
    }
    return {
      ...window,
      status: "failed",
      translations: [],
      memoryCandidates: [],
      error: `validation failed after one targeted repair: ${failureMessage(validation.failures)}`,
    };
  });
  return {
    windows: validatedWindows,
    responseErrors: initial.responseErrors,
    repairRuns: [repair.run],
  };
}

export async function runTranslationBatch(
  input: TranslationBatchInput,
): Promise<TranslationBatchResult> {
  nonempty(input.request.requestId, "requestId");
  nonempty(input.snapshot.id, "snapshotId");
  let submission: FinalizeTranslationBatchArgs | undefined;
  let duplicateTerminalCalls = 0;
  const responseProtocol = input.responseProtocol ?? "typed_tool";
  const prepared = prepareTranslationRequest(input, {
    onFinalize: async (args) => {
      if (submission !== undefined) {
        duplicateTerminalCalls += 1;
        return { accepted: true };
      }
      input.budget.consume("translationToolCalls", 1);
      submission = structuredClone(args);
      return { accepted: true };
    },
  });
  const run = await new PiRuntime().run({
    systemPrompt: prepared.systemPrompt,
    prompt: prepared.prompt,
    phase: "translation",
    model: input.model,
    tools: prepared.tools,
    budget: input.budget,
    terminateTools: responseProtocol === "typed_tool" ? ["finalize_translation_batch"] : [],
    // A malformed typed-tool call gets one corrective turn. More turns have no
    // new evidence and only turn a provider-format error into a long stall.
    maxTurns: responseProtocol === "typed_tool" ? 2 : 1,
    signal: input.signal,
    deadlineMs: input.deadlineMs,
    thinkingLevel: input.thinkingLevel,
  }, input.streamFn);
  const framedErrors: string[] = [];
  if (responseProtocol === "framed_text") {
    input.budget.consume("translationToolCalls", 1);
    const protocol = prepared.framedProtocol;
    if (protocol === undefined) {
      framedErrors.push("framed response protocol was not prepared");
    } else {
      const parsed = parseFramedTranslationResponse(lastAssistantText(run), protocol);
      framedErrors.push(...parsed.errors);
      if (parsed.errors.length === 0) {
        submission = framedSubmission(input, parsed.translations);
      }
    }
  }
  const validated = validateSubmission(input, submission);
  validated.responseErrors.push(...framedErrors);
  if (duplicateTerminalCalls > 0) {
    validated.responseErrors.push(
      `multiple terminating submissions rejected: ${duplicateTerminalCalls + 1}`,
    );
  }
  const checked = await validateAndRepair(input, validated);
  return {
    requestId: input.request.requestId,
    snapshotId: input.snapshot.id,
    ...checked,
    run,
  };
}
