import type { StreamFn } from "@earendil-works/pi-agent-core";
import type { Model } from "@earendil-works/pi-ai";

import type { ProvisionalSnapshot } from "../domain/provisional-snapshot.js";
import type { V4Block } from "../domain/types.js";
import type { BudgetLedger } from "../kernel/budget.js";
import type {
  CandidateCollector,
  TranslationCandidate,
} from "../tools/candidate-collector.js";
import {
  RepairTools,
  type ValidationFailure,
} from "../tools/repair-tools.js";
import { PiRuntime, type PiRunResult } from "./pi-runtime.js";

interface RepairInput {
  blocks: readonly V4Block[];
  failedCandidate: TranslationCandidate;
  failures: readonly ValidationFailure[];
  snapshot: ProvisionalSnapshot;
  collector: CandidateCollector;
  budget: BudgetLedger;
  model: Model<any>;
  streamFn: StreamFn;
  signal?: AbortSignal;
  deadlineMs?: number;
}

export interface RepairOutcome {
  candidate?: TranslationCandidate;
  run: PiRunResult;
}

export class Repairer {
  constructor(private readonly runtime: PiRuntime) {}

  async repair(input: RepairInput): Promise<RepairOutcome> {
    const before = input.collector.translations().length;
    const tools = new RepairTools({
      budget: input.budget,
      targetBlocks: input.blocks,
      failures: input.failures,
      collector: input.collector,
    });
    const prompt = [
      "VALIDATION FAILURES",
      JSON.stringify(input.failures),
      "SOURCE BLOCKS",
      input.blocks.map((block) =>
        `[${block.id}]\n${block.sourceText}`,
      ).join("\n\n"),
      "FAILED CANDIDATE",
      JSON.stringify(input.failedCandidate.translations),
      "NECESSARY PROVISIONAL FACTS",
      JSON.stringify([
        ...input.snapshot.narrativeFacts,
        ...input.snapshot.translatorFacts,
      ]),
      "Submit one complete corrected island with submit_repaired_translation.",
    ].join("\n\n");
    const run = await this.runtime.run({
      systemPrompt: [
        "Repair a Chinese literary translation only for the typed validation failures.",
        "Preserve all unaffected meaning and paragraph structure.",
        "Do not explain. Call submit_repaired_translation exactly once with the complete island.",
      ].join("\n"),
      prompt,
      phase: "repair",
      model: input.model,
      tools: tools.specs(),
      budget: input.budget,
      terminateTools: ["submit_repaired_translation"],
      signal: input.signal,
      deadlineMs: input.deadlineMs,
    }, input.streamFn);
    return {
      candidate: input.collector.translations().slice(before).at(-1),
      run,
    };
  }
}
