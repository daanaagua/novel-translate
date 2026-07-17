import type { StreamFn } from "@earendil-works/pi-agent-core";
import type { Model } from "@earendil-works/pi-ai";

import type { ProvisionalSnapshot } from "../domain/provisional-snapshot.js";
import type { StableTerm, V4Block } from "../domain/types.js";
import type { BudgetLedger } from "../kernel/budget.js";
import type {
  CandidateCollector,
  ResolutionCandidate,
  TranslationCandidate,
} from "../tools/candidate-collector.js";
import {
  TranslationTools,
  type StyleState,
} from "../tools/translation-tools.js";
import {
  TranslationValidator,
  type TranslationValidation,
} from "../validators/translation-validator.js";
import type { PiRunResult } from "./pi-runtime.js";
import { PiRuntime } from "./pi-runtime.js";
import { Repairer, type RepairOutcome } from "./repairer.js";

export interface TranslationIsland {
  islandId: string;
  chapterId: string;
  chapterTitle: string | null;
  blocks: V4Block[];
}

export interface TranslateIslandInput {
  island: TranslationIsland;
  model: Model<any>;
  streamFn: StreamFn;
  budget: BudgetLedger;
  collector: CandidateCollector;
  stableTerms: readonly StableTerm[];
  snapshot: ProvisionalSnapshot;
  styleState: StyleState;
  previousActiveTail: string;
  signal?: AbortSignal;
  deadlineMs?: number;
}

export interface TranslationOutcome {
  island: TranslationIsland;
  initialPrompt: string;
  usedResolutionIds: string[];
  candidate?: TranslationCandidate;
  validation: TranslationValidation;
  run: PiRunResult;
  repairRun?: PiRunResult;
  repaired: boolean;
  humanRequired: boolean;
}

export function splitIntoChapterIslands(
  sourceBlocks: readonly V4Block[],
): TranslationIsland[] {
  const blocks = [...sourceBlocks].sort((left, right) =>
    left.globalIndex - right.globalIndex || left.blockIndex - right.blockIndex,
  );
  const islands: TranslationIsland[] = [];
  for (const block of blocks) {
    const chapterId = block.chapterId ?? `chapter-at-${block.globalIndex}`;
    const current = islands.at(-1);
    if (current === undefined || current.chapterId !== chapterId) {
      islands.push({
        islandId: `${chapterId}:${block.globalIndex}`,
        chapterId,
        chapterTitle: block.chapterTitle,
        blocks: [{ ...block }],
      });
    } else {
      current.blocks.push({ ...block });
    }
  }
  return islands;
}

function factsAsResolutions(snapshot: ProvisionalSnapshot): ResolutionCandidate[] {
  return [...snapshot.narrativeFacts, ...snapshot.translatorFacts].map((fact) => ({
    questionId: fact.questionId,
    verdict: fact.verdict,
    confidence: fact.confidence,
    evidenceIds: [...fact.evidenceIds],
    unresolved: "",
  }));
}

function relevantTerms(
  blocks: readonly V4Block[],
  terms: readonly StableTerm[],
): StableTerm[] {
  const source = blocks.map((block) => block.sourceText).join("\n").toLocaleLowerCase();
  const seen = new Set<string>();
  return terms.filter((term) => {
    const match = source.includes(term.sourceForm.toLocaleLowerCase())
      || source.includes(term.canonicalSource.toLocaleLowerCase());
    const key = `${term.conceptId}\0${term.sourceForm}\0${term.target}`;
    if (!match || seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

function emptyCandidate(): TranslationCandidate {
  return { translations: [], notes: [], repaired: false };
}

export class Translator {
  readonly #validator = new TranslationValidator();
  readonly #repairer: Repairer;

  constructor(private readonly runtime: PiRuntime) {
    this.#repairer = new Repairer(runtime);
  }

  async translateIsland(input: TranslateIslandInput): Promise<TranslationOutcome> {
    const terms = relevantTerms(input.island.blocks, input.stableTerms);
    const resolutions = factsAsResolutions(input.snapshot);
    const tools = new TranslationTools({
      budget: input.budget,
      targetBlocks: input.island.blocks,
      collector: input.collector,
      stableTerms: terms,
      resolvedEvidence: resolutions,
      styleState: input.styleState,
    });
    const before = input.collector.translations().length;
    const initialPrompt = this.#initialPrompt(input, terms);
    const run = await this.runtime.run({
      systemPrompt: [
        "Translate the complete source island into polished, accurate Chinese literary prose.",
        "Preserve meaning, ambiguity, paragraph structure, voice, and all block boundaries.",
        "Use translator-global facts only to disambiguate wording; do not add facts unavailable to the narrator.",
        "Use typed tools only. Submit every block exactly once with finalize_translation.",
      ].join("\n"),
      prompt: initialPrompt,
      phase: "translation",
      model: input.model,
      tools: tools.specs(),
      budget: input.budget,
      terminateTools: ["finalize_translation"],
      signal: input.signal,
      deadlineMs: input.deadlineMs,
    }, input.streamFn);

    let candidate = input.collector.translations().slice(before).at(-1);
    let validation = this.#validator.validate(
      input.island.blocks,
      candidate ?? emptyCandidate(),
    );
    let repair: RepairOutcome | undefined;
    if (!validation.valid && candidate !== undefined) {
      repair = await this.#repairer.repair({
        blocks: input.island.blocks,
        failedCandidate: candidate,
        failures: validation.failures,
        snapshot: input.snapshot,
        collector: input.collector,
        budget: input.budget,
        model: input.model,
        streamFn: input.streamFn,
        signal: input.signal,
        deadlineMs: input.deadlineMs,
      });
      if (repair.candidate !== undefined) {
        candidate = repair.candidate;
        validation = this.#validator.validate(input.island.blocks, candidate);
      }
    }
    return {
      island: input.island,
      initialPrompt,
      usedResolutionIds: tools.usedResolutionIds(),
      candidate,
      validation,
      run,
      repairRun: repair?.run,
      repaired: candidate?.repaired ?? false,
      humanRequired: !validation.valid,
    };
  }

  #initialPrompt(input: TranslateIslandInput, terms: readonly StableTerm[]): string {
    const highImpactIds = new Set(input.snapshot.questions
      .filter((question) => question.impact === "high" || question.mandatory)
      .map((question) => question.questionId));
    const facts = [...input.snapshot.narrativeFacts, ...input.snapshot.translatorFacts]
      .filter((fact) => highImpactIds.has(fact.questionId));
    return [
      `ISLAND ${input.island.islandId}`,
      `CHAPTER ${input.island.chapterId} ${input.island.chapterTitle ?? ""}`,
      `POSITION ${input.island.blocks[0]?.globalIndex}-${input.island.blocks.at(-1)?.globalIndex}`,
      "SOURCE BLOCKS",
      input.island.blocks.map((block) =>
        `[${block.id}]\n${block.sourceText}`,
      ).join("\n\n"),
      "RELEVANT STABLE TERMS",
      terms.map((term) => `${term.sourceForm} => ${term.target}`).join("\n") || "(none)",
      "PREVIOUS ACTIVE TAIL",
      input.previousActiveTail || "(none)",
      "HIGH-IMPACT PROVISIONAL FACTS",
      JSON.stringify(facts),
      "STYLE STATE",
      JSON.stringify(input.styleState),
      "Translate all listed blocks. Retrieve a resolution only when its full evidence-bound wording is needed.",
    ].join("\n\n");
  }
}

export async function mapWithConcurrency<T, R>(
  values: readonly T[],
  limit: number,
  worker: (value: T, index: number) => Promise<R>,
): Promise<R[]> {
  if (!Number.isSafeInteger(limit) || limit < 1) {
    throw new TypeError("concurrency limit must be a positive integer");
  }
  const results = new Array<R>(values.length);
  let next = 0;
  const runners = Array.from(
    { length: Math.min(limit, values.length) },
    async () => {
      while (true) {
        const index = next;
        next += 1;
        if (index >= values.length) {
          return;
        }
        results[index] = await worker(values[index] as T, index);
      }
    },
  );
  await Promise.all(runners);
  return results;
}
