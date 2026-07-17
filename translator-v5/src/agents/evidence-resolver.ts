import type { Model } from "@earendil-works/pi-ai";
import type { StreamFn } from "@earendil-works/pi-agent-core";

import {
  buildProvisionalSnapshot,
  type ProvisionalSnapshot,
} from "../domain/provisional-snapshot.js";
import type { V4Block } from "../domain/types.js";
import { BudgetLedger } from "../kernel/budget.js";
import type { CandidateCollector } from "../tools/candidate-collector.js";
import type { ResearchTools } from "../tools/research-tools.js";
import type { PiRunResult } from "./pi-runtime.js";
import { PiRuntime } from "./pi-runtime.js";
import type { QuestionScout } from "./question-scout.js";

interface EvidenceResolverInput {
  scout: QuestionScout;
  tools: ResearchTools;
  collector: CandidateCollector;
  budget: BudgetLedger;
  model: Model<any>;
  streamFn: StreamFn;
  targetBlocks: readonly V4Block[];
  protocolVersion: string;
  signal?: AbortSignal;
  deadlineMs?: number;
}

export interface ResearchOutcome {
  snapshot: ProvisionalSnapshot;
  run: PiRunResult;
  metrics: {
    offTargetEvidenceChars: number;
    researchToolCalls: number;
    modelCalls: number;
    unresolvedQuestions: number;
    questionGatePassed: boolean;
  };
}

export class EvidenceResolver {
  constructor(private readonly runtime: PiRuntime) {}

  async run(input: EvidenceResolverInput): Promise<ResearchOutcome> {
    const mandatory = input.scout.mandatoryQuestions();
    const existing = new Set(input.collector.questions().map((item) => item.questionId));
    if (mandatory.some((item) => !existing.has(item.questionId))) {
      throw new Error("mandatory questions must be registered before resolver execution");
    }

    const systemPrompt = input.scout.systemPrompt();
    const run = await this.runtime.run({
      systemPrompt,
      prompt: input.scout.prompt(),
      phase: "research",
      model: input.model,
      tools: input.tools.specs(),
      budget: input.budget,
      terminateTools: ["submit_resolution", "finish_research"],
      signal: input.signal,
      deadlineMs: input.deadlineMs,
    }, input.streamFn);

    const questions = input.collector.questions();
    const questionGatePassed = input.scout.assertSubmissionGate(
      run.toolNames,
      questions,
    );
    const resolvedIds = new Set(
      input.collector.resolutions().map((resolution) => resolution.questionId),
    );
    const reported = input.collector.researchStatus();
    const unresolvedIds = new Set(reported.unresolvedQuestionIds);
    for (const questionId of resolvedIds) {
      unresolvedIds.delete(questionId);
    }
    for (const question of questions) {
      if (!resolvedIds.has(question.questionId)) {
        unresolvedIds.add(question.questionId);
      }
    }
    input.collector.finishResearch([...unresolvedIds]);

    const snapshot = buildProvisionalSnapshot({
      protocolVersion: input.protocolVersion,
      systemPrompt,
      model: input.model,
      targetBlocks: input.targetBlocks,
      questions,
      resolutions: input.collector.resolutions(),
      unresolvedQuestionIds: [...unresolvedIds],
      evidence: input.tools.issuedEvidence(),
    });
    const consumed = input.budget.snapshot();
    return {
      snapshot,
      run,
      metrics: {
        offTargetEvidenceChars: consumed.evidenceChars,
        researchToolCalls: consumed.researchToolCalls,
        modelCalls: run.modelCalls,
        unresolvedQuestions: snapshot.unresolved.length,
        questionGatePassed,
      },
    };
  }
}
