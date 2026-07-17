import { createHash } from "node:crypto";
import { mkdirSync } from "node:fs";
import { join } from "node:path";

import type { StreamFn } from "@earendil-works/pi-agent-core";
import type { Model } from "@earendil-works/pi-ai";

import { EvidenceResolver, type ResearchOutcome } from "./agents/evidence-resolver.js";
import { PiRuntime } from "./agents/pi-runtime.js";
import { QuestionScout } from "./agents/question-scout.js";
import {
  mapWithConcurrency,
  splitIntoChapterIslands,
  Translator,
  type TranslationOutcome,
} from "./agents/translator.js";
import {
  buildProvisionalSnapshot,
  type ProvisionalSnapshot,
} from "./domain/provisional-snapshot.js";
import type { RunStatus, StableTerm, V4Block } from "./domain/types.js";
import { EvidenceIndex } from "./index/evidence-index.js";
import { BudgetLedger } from "./kernel/budget.js";
import { MemoryEventLog } from "./kernel/event-log.js";
import { RunLease } from "./kernel/run-lease.js";
import {
  joinTranslations,
  renderBilingual,
  renderTranslation,
  type PilotTranslation,
} from "./report.js";
import { PilotStore, type PilotArtifactPaths } from "./storage/pilot-store.js";
import { V4ReadAdapter } from "./storage/v4-read-adapter.js";
import { CandidateCollector } from "./tools/candidate-collector.js";
import { ResearchTools, type SubjectRef } from "./tools/research-tools.js";

const SOFT_DEADLINE_MS = 15 * 60 * 1000;
const DEFAULT_HARD_DEADLINE_MS = 30 * 60 * 1000;
const OUTPUT_PREFIX = "Typhon_v5_agent";

const ALLOWED_TRANSITIONS: Record<RunStatus, readonly RunStatus[]> = {
  created: ["indexed", "failed"],
  indexed: ["researched", "failed"],
  researched: ["translating", "failed"],
  translating: ["validating", "failed"],
  validating: [
    "completed",
    "completed_with_warnings",
    "human_required",
    "failed",
  ],
  completed: [],
  completed_with_warnings: [],
  human_required: [],
  failed: [],
};

interface StateEntry {
  status: RunStatus;
  elapsedMs: number;
}

class PilotStateMachine {
  #status: RunStatus = "created";
  readonly #started = performance.now();
  readonly #history: StateEntry[] = [{ status: "created", elapsedMs: 0 }];

  get status(): RunStatus {
    return this.#status;
  }

  transition(next: RunStatus): void {
    if (!ALLOWED_TRANSITIONS[this.#status].includes(next)) {
      throw new Error(`invalid pilot transition: ${this.#status} -> ${next}`);
    }
    this.#status = next;
    this.#history.push({ status: next, elapsedMs: performance.now() - this.#started });
  }

  fail(): void {
    if (ALLOWED_TRANSITIONS[this.#status].includes("failed")) {
      this.transition("failed");
    }
  }

  history(): StateEntry[] {
    return this.#history.map((entry) => ({ ...entry }));
  }
}

export interface PilotMetrics {
  targetBlocks: number;
  sourceChars: number;
  translations: number;
  narrativeTableReads: 0;
  modelCalls: number;
  offTargetEvidenceChars: number;
  researchToolCalls: number;
  translationToolCalls: number;
  wallTimeMs: number;
  softDeadlineMs: number;
  hardDeadlineMs: number;
  leaseReleased: boolean;
  budget: Readonly<Record<string, number>>;
  validationFailures: number;
  degradedReasons: string[];
}

export interface PilotAudit {
  dataProfile: "cold-preview";
  status: RunStatus;
  stateHistory: StateEntry[];
  model: { provider: string; id: string };
  target: {
    globalIndexes: number[];
    sourceHashes: Record<string, string>;
  };
  snapshot: ProvisionalSnapshot;
  toolCalls: {
    research: string[];
    translation: Array<{ islandId: string; tools: string[]; repairTools: string[] }>;
  };
  validations: Array<{
    islandId: string;
    repaired: boolean;
    valid: boolean;
    failures: Array<{ code: string; blockId?: string; message: string }>;
  }>;
  budget: Readonly<Record<string, number>>;
  degradedReasons: string[];
}

export interface RunPilotOptions {
  dbPath: string;
  outputDir: string;
  globalIndexes: readonly number[];
  model: Model<any>;
  streamFn: StreamFn;
  runtime?: PiRuntime;
  translationConcurrency?: number;
  hardDeadlineMs?: number;
  protocolVersion?: string;
  outputPrefix?: string;
}

export interface PilotResult {
  status: RunStatus;
  translations: PilotTranslation[];
  snapshot: ProvisionalSnapshot;
  metrics: PilotMetrics;
  audit: PilotAudit;
  artifacts: PilotArtifactPaths;
  leasePath: string;
}

export interface PilotPreflight {
  targetBlocks: number;
  sourceChars: number;
  sourceTokens: number;
  globalIndexes: number[];
  narrativeTableReads: 0;
  maxModelCalls: 20;
  maxOffTargetEvidenceChars: 12_000;
  softDeadlineMs: number;
  hardDeadlineMs: number;
}

function normalizeIndexes(values: readonly number[]): number[] {
  const indexes = [...new Set(values)].sort((left, right) => left - right);
  if (indexes.length === 0 || indexes.some((value) =>
    !Number.isSafeInteger(value) || value < 0)) {
    throw new TypeError("globalIndexes must contain non-negative safe integers");
  }
  return indexes;
}

export function preflightPilot(
  dbPath: string,
  globalIndexes: readonly number[],
  hardDeadlineMs = DEFAULT_HARD_DEADLINE_MS,
): PilotPreflight {
  const indexes = normalizeIndexes(globalIndexes);
  const adapter = new V4ReadAdapter(dbPath);
  try {
    const blocks = adapter.loadBlocks(indexes);
    if (blocks.length !== indexes.length) {
      throw new Error(`requested ${indexes.length} blocks but found ${blocks.length}`);
    }
    return {
      targetBlocks: blocks.length,
      sourceChars: blocks.reduce((total, block) => total + block.sourceText.length, 0),
      sourceTokens: blocks.reduce((total, block) => total + block.tokenCount, 0),
      globalIndexes: indexes,
      narrativeTableReads: 0,
      maxModelCalls: 20,
      maxOffTargetEvidenceChars: 12_000,
      softDeadlineMs: SOFT_DEADLINE_MS,
      hardDeadlineMs,
    };
  } finally {
    adapter.close();
  }
}

function buildSubjects(
  terms: readonly StableTerm[],
  targetBlocks: readonly V4Block[],
): { subjects: SubjectRef[]; unresolved: SubjectRef[] } {
  const source = targetBlocks.map((block) => block.sourceText).join("\n").toLocaleLowerCase();
  const grouped = new Map<string, { forms: Set<string> }>();
  for (const term of terms) {
    if (!source.includes(term.sourceForm.toLocaleLowerCase())
      && !source.includes(term.canonicalSource.toLocaleLowerCase())) {
      continue;
    }
    const current = grouped.get(term.conceptId) ?? { forms: new Set() };
    current.forms.add(term.sourceForm);
    current.forms.add(term.canonicalSource);
    grouped.set(term.conceptId, current);
  }
  const subjects = [...grouped.entries()].map(([subjectId, value]) => ({
    subjectId,
    forms: [...value.forms].filter(Boolean),
  }));
  const knownForms = new Set(subjects.flatMap((subject) => subject.forms)
    .map((form) => form.toLocaleLowerCase()));
  for (const form of [...knownForms]) {
    for (const component of form.split(/[^a-z]+/u).filter(Boolean)) {
      knownForms.add(component);
    }
  }
  const commonCapitalized = new Set([
    "a", "an", "and", "as", "at", "but", "for", "from", "he", "her",
    "his", "i", "if", "in", "it", "its", "my", "no", "not", "of",
    "on", "or", "our", "she", "so", "that", "the", "their", "then",
    "there", "they", "this", "to", "we", "when", "where", "which",
    "who", "with", "you", "ahead", "because", "little", "night", "one",
    "only", "once", "time",
  ]);
  const unknownNames: SubjectRef[] = [];
  const seenUnknown = new Set<string>();
  const originalSource = targetBlocks.map((block) => block.sourceText).join("\n");
  for (const match of originalSource.matchAll(/\b[A-Z][A-Za-z'’-]{2,}\b/gu)) {
    const form = match[0];
    const normalized = form
      .replace(/[’']s$/u, "")
      .toLocaleLowerCase();
    const before = originalSource.slice(0, match.index).trimEnd().at(-1);
    const atSentenceStart = before === undefined || /[.!?]/u.test(before);
    if (knownForms.has(normalized) || commonCapitalized.has(normalized)
      || seenUnknown.has(normalized) || atSentenceStart) {
      continue;
    }
    seenUnknown.add(normalized);
    unknownNames.push({
      subjectId: `local-${createHash("sha256").update(normalized).digest("hex").slice(0, 12)}`,
      forms: [form],
    });
    if (unknownNames.length >= 6) {
      break;
    }
  }
  subjects.push(...unknownNames);
  return {
    subjects,
    unresolved: unknownNames,
  };
}

function runKey(options: RunPilotOptions, indexes: readonly number[]): string {
  return createHash("sha256")
    .update(options.dbPath)
    .update("\0")
    .update(indexes.join(","))
    .update("\0")
    .update(options.outputDir)
    .digest("hex")
    .slice(0, 20);
}

export async function runPilot(options: RunPilotOptions): Promise<PilotResult> {
  const indexes = normalizeIndexes(options.globalIndexes);
  const hardDeadlineMs = options.hardDeadlineMs ?? DEFAULT_HARD_DEADLINE_MS;
  const concurrency = options.translationConcurrency ?? 2;
  if (!Number.isFinite(hardDeadlineMs) || hardDeadlineMs <= 0) {
    throw new TypeError("hardDeadlineMs must be positive");
  }
  const startedAt = performance.now();
  const state = new PilotStateMachine();
  const budget = new BudgetLedger();
  const runtime = options.runtime ?? new PiRuntime();
  const eventLog = new MemoryEventLog();
  const degradedReasons: string[] = [];
  mkdirSync(join(options.outputDir, ".pilot"), { recursive: true });
  const leasePath = join(options.outputDir, ".pilot", `${runKey(options, indexes)}.lock`);
  const lease = RunLease.acquire(leasePath, runKey(options, indexes));
  const controller = new AbortController();
  const deadline = setTimeout(() => controller.abort(new Error("pilot hard deadline exceeded")), hardDeadlineMs);
  let adapter: V4ReadAdapter | undefined;
  let evidenceIndex: EvidenceIndex | undefined;
  let result: PilotResult | undefined;

  try {
    adapter = new V4ReadAdapter(options.dbPath);
    const allBlocks = adapter.loadBlocks();
    const targetByIndex = new Map(allBlocks.map((block) => [block.globalIndex, block]));
    const targetBlocks = indexes.map((index) => targetByIndex.get(index))
      .filter((block): block is V4Block => block !== undefined);
    if (targetBlocks.length !== indexes.length) {
      throw new Error(`requested ${indexes.length} blocks but found ${targetBlocks.length}`);
    }
    const stableTerms = adapter.loadStableTerms();
    evidenceIndex = EvidenceIndex.fromBlocks(allBlocks);
    state.transition("indexed");

    const subjectCatalog = buildSubjects(stableTerms, targetBlocks);
    const mandatory = QuestionScout.forcedQuestionsForUnresolvedNames(
      subjectCatalog.unresolved,
    );
    const collector = new CandidateCollector();
    const scout = new QuestionScout({
      targetBlocks,
      subjects: subjectCatalog.subjects,
      mandatoryQuestions: mandatory,
    });
    const researchTools = new ResearchTools({
      budget,
      evidenceIndex,
      targetGlobalIndex: Math.min(...indexes),
      targetBlockIndexes: indexes,
      subjects: subjectCatalog.subjects,
      stableTerms,
      questions: scout.mandatoryQuestions(),
      collector,
    });
    let research: ResearchOutcome | undefined;
    let snapshot: ProvisionalSnapshot;
    try {
      research = await new EvidenceResolver(runtime).run({
        scout,
        tools: researchTools,
        collector,
        budget,
        model: options.model,
        streamFn: options.streamFn,
        targetBlocks,
        protocolVersion: options.protocolVersion ?? "v5-agent-kernel-pilot-1",
        signal: controller.signal,
      });
      snapshot = research.snapshot;
      if (!research.metrics.questionGatePassed) {
        degradedReasons.push("question scout skipped submit_questions gate; retained bounded evidence results");
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      degradedReasons.push(`research fallback: ${message}`);
      const questions = collector.questions();
      collector.finishResearch(questions.map((question) => question.questionId));
      snapshot = buildProvisionalSnapshot({
        protocolVersion: options.protocolVersion ?? "v5-agent-kernel-pilot-1",
        systemPrompt: scout.systemPrompt(),
        model: options.model,
        targetBlocks,
        questions,
        resolutions: collector.resolutions(),
        unresolvedQuestionIds: questions.map((question) => question.questionId),
        evidence: researchTools.issuedEvidence(),
      });
    }
    state.transition("researched");
    state.transition("translating");

    const translator = new Translator(runtime);
    const islands = splitIntoChapterIslands(targetBlocks);
    const translationOutcomes = await mapWithConcurrency(
      islands,
      concurrency,
      (island) => translator.translateIsland({
        island,
        model: options.model,
        streamFn: options.streamFn,
        budget,
        collector: new CandidateCollector(),
        stableTerms,
        snapshot,
        styleState: {
          register: "literary, precise, restrained",
          dialogueQuotes: "Chinese curly double quotes",
          paragraphPolicy: "preserve source paragraph boundaries",
        },
        previousActiveTail: "",
        signal: controller.signal,
      }),
    );
    state.transition("validating");

    const translationsById = new Map<string, string>();
    for (const outcome of translationOutcomes) {
      for (const translation of outcome.candidate?.translations ?? []) {
        translationsById.set(translation.blockId, translation.text);
      }
    }
    const translations = joinTranslations(targetBlocks, translationsById);
    const validationFailures = translationOutcomes.reduce(
      (total, outcome) => total + outcome.validation.failures.length,
      0,
    );
    const humanRequired = translationOutcomes.some((outcome) => outcome.humanRequired)
      || translations.length !== targetBlocks.length;
    const wallTimeMs = performance.now() - startedAt;
    if (wallTimeMs > SOFT_DEADLINE_MS) {
      degradedReasons.push("soft deadline exceeded");
    }
    if (humanRequired) {
      state.transition("human_required");
    } else if (degradedReasons.length > 0) {
      state.transition("completed_with_warnings");
    } else {
      state.transition("completed");
    }

    const consumed = budget.snapshot();
    const metrics: PilotMetrics = {
      targetBlocks: targetBlocks.length,
      sourceChars: targetBlocks.reduce((total, block) => total + block.sourceText.length, 0),
      translations: translations.length,
      narrativeTableReads: 0,
      modelCalls: consumed.modelCalls,
      offTargetEvidenceChars: consumed.evidenceChars,
      researchToolCalls: consumed.researchToolCalls,
      translationToolCalls: consumed.translationToolCalls,
      wallTimeMs,
      softDeadlineMs: SOFT_DEADLINE_MS,
      hardDeadlineMs,
      leaseReleased: true,
      budget: consumed,
      validationFailures,
      degradedReasons: [...degradedReasons],
    };
    const audit: PilotAudit = {
      dataProfile: "cold-preview",
      status: state.status,
      stateHistory: state.history(),
      model: { provider: options.model.provider, id: options.model.id },
      target: {
        globalIndexes: indexes,
        sourceHashes: Object.fromEntries(
          targetBlocks.map((block) => [block.id, block.sourceHash]),
        ),
      },
      snapshot,
      toolCalls: {
        research: research?.run.toolNames ?? [],
        translation: translationOutcomes.map((outcome) => ({
          islandId: outcome.island.islandId,
          tools: [...outcome.run.toolNames],
          repairTools: [...(outcome.repairRun?.toolNames ?? [])],
        })),
      },
      validations: translationOutcomes.map((outcome) => ({
        islandId: outcome.island.islandId,
        repaired: outcome.repaired,
        valid: outcome.validation.valid,
        failures: outcome.validation.failures.map((failure) => ({
          code: failure.code,
          ...(failure.blockId === undefined ? {} : { blockId: failure.blockId }),
          message: failure.message,
        })),
      })),
      budget: consumed,
      degradedReasons: [...degradedReasons],
    };
    const artifacts = new PilotStore(options.outputDir).write(
      options.outputPrefix ?? OUTPUT_PREFIX,
      {
        translation: renderTranslation(translations),
        bilingual: renderBilingual(translations),
        audit,
        metrics,
      },
    );
    result = {
      status: state.status,
      translations,
      snapshot,
      metrics,
      audit,
      artifacts,
      leasePath,
    };
  } catch (error) {
    state.fail();
    eventLog.append("degraded", {
      message: error instanceof Error ? error.message : String(error),
    });
    throw error;
  } finally {
    clearTimeout(deadline);
    evidenceIndex?.close();
    adapter?.close();
    lease.release();
    if (result !== undefined) {
      result.metrics.leaseReleased = true;
    }
  }

  return result as PilotResult;
}
