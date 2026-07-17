import type { StableTerm, V4Block } from "../domain/types.js";
import { BudgetLedger } from "../kernel/budget.js";
import {
  CandidateCollector,
  type ResolutionCandidate,
  type TranslationCandidate,
} from "./candidate-collector.js";
import {
  assertNotAborted,
  Type,
  type TypedToolSpec,
} from "./tool-spec.js";

export interface StyleState {
  [key: string]: string;
}

interface TranslationToolsOptions {
  budget: BudgetLedger;
  targetBlocks: readonly V4Block[];
  collector: CandidateCollector;
  stableTerms: readonly StableTerm[];
  resolvedEvidence: readonly ResolutionCandidate[];
  styleState: StyleState;
  /** Kept only to prove candidate submission cannot invoke active-state commits. */
  commitActiveState?: () => void;
}

export class TranslationTools {
  readonly #budget: BudgetLedger;
  readonly #targetBlocks: readonly V4Block[];
  readonly #collector: CandidateCollector;
  readonly #stableTerms: readonly StableTerm[];
  readonly #resolvedEvidence: readonly ResolutionCandidate[];
  readonly #styleState: StyleState;

  constructor(options: TranslationToolsOptions) {
    this.#budget = options.budget;
    this.#targetBlocks = options.targetBlocks.map((item) => ({ ...item }));
    this.#collector = options.collector;
    this.#stableTerms = options.stableTerms.map((item) => ({ ...item }));
    this.#resolvedEvidence = options.resolvedEvidence.map((item) => ({
      ...item,
      evidenceIds: [...item.evidenceIds],
    }));
    this.#styleState = { ...options.styleState };
    // commitActiveState is intentionally not retained.
  }

  async getRequiredContext(
    _args: Record<string, never> = {},
    signal?: AbortSignal,
  ): Promise<{ blocks: V4Block[]; stableTerms: StableTerm[] }> {
    assertNotAborted(signal);
    this.#budget.consume("translationToolCalls", 1);
    return {
      blocks: this.#targetBlocks.map((item) => ({ ...item })),
      stableTerms: this.#stableTerms.map((item) => ({ ...item })),
    };
  }

  async inspectLocalContinuity(
    args: { blockId: string; radius: number },
    signal?: AbortSignal,
  ): Promise<{ blocks: V4Block[] }> {
    assertNotAborted(signal);
    const index = this.#targetBlocks.findIndex((item) => item.id === args.blockId);
    if (index < 0) {
      throw new Error(`unknown target block: ${args.blockId}`);
    }
    if (!Number.isSafeInteger(args.radius) || args.radius < 0 || args.radius > 2) {
      throw new TypeError("radius must be an integer from 0 to 2");
    }
    this.#budget.consume("translationToolCalls", 1);
    return {
      blocks: this.#targetBlocks
        .slice(Math.max(0, index - args.radius), index + args.radius + 1)
        .map((item) => ({ ...item })),
    };
  }

  async retrieveResolvedEvidence(
    args: { questionIds: string[] },
    signal?: AbortSignal,
  ): Promise<{ resolutions: ResolutionCandidate[] }> {
    assertNotAborted(signal);
    const ids = new Set(args.questionIds);
    this.#budget.consume("translationToolCalls", 1);
    return {
      resolutions: this.#resolvedEvidence
        .filter((item) => ids.size === 0 || ids.has(item.questionId))
        .map((item) => ({ ...item, evidenceIds: [...item.evidenceIds] })),
    };
  }

  async inspectStyleState(
    _args: Record<string, never> = {},
    signal?: AbortSignal,
  ): Promise<{ styleState: StyleState }> {
    assertNotAborted(signal);
    this.#budget.consume("translationToolCalls", 1);
    return { styleState: { ...this.#styleState } };
  }

  async finalizeTranslation(
    args: Omit<TranslationCandidate, "repaired">,
    signal?: AbortSignal,
  ): Promise<{ accepted: true }> {
    assertNotAborted(signal);
    this.#validateTranslations(args.translations);
    this.#budget.consume("translationToolCalls", 1);
    this.#collector.addTranslation({
      translations: args.translations,
      notes: Array.isArray(args.notes) ? args.notes : [],
      repaired: false,
    });
    return { accepted: true };
  }

  specs(): TypedToolSpec[] {
    const Empty = Type.Object({}, { additionalProperties: false });
    return [
      {
        name: "get_required_context",
        label: "Get required context",
        description: "Read immutable target source blocks and stable terminology.",
        phase: "translation",
        parameters: Empty,
        execute: (args, signal) => this.getRequiredContext(
          args as Record<string, never>, signal,
        ),
      },
      {
        name: "inspect_local_continuity",
        label: "Inspect local continuity",
        description: "Read neighboring target blocks with a radius no larger than two.",
        phase: "translation",
        parameters: Type.Object({
          blockId: Type.String(),
          radius: Type.Integer({ minimum: 0, maximum: 2 }),
        }),
        execute: (args, signal) => this.inspectLocalContinuity(
          args as { blockId: string; radius: number }, signal,
        ),
      },
      {
        name: "retrieve_resolved_evidence",
        label: "Retrieve resolved evidence",
        description: "Read provisional evidence-bound research resolutions.",
        phase: "translation",
        parameters: Type.Object({ questionIds: Type.Array(Type.String()) }),
        execute: (args, signal) => this.retrieveResolvedEvidence(
          args as { questionIds: string[] }, signal,
        ),
      },
      {
        name: "inspect_style_state",
        label: "Inspect style state",
        description: "Read the compact, deterministic style control state.",
        phase: "translation",
        parameters: Empty,
        execute: (args, signal) => this.inspectStyleState(
          args as Record<string, never>, signal,
        ),
      },
      {
        name: "finalize_translation",
        label: "Finalize translation",
        description: "Submit a complete run-local translation candidate for validation.",
        phase: "translation",
        parameters: Type.Object({
          translations: Type.Array(Type.Object({
            blockId: Type.String(),
            text: Type.String(),
          })),
          notes: Type.Array(Type.String()),
        }),
        execute: (args, signal) => this.finalizeTranslation(
          args as Omit<TranslationCandidate, "repaired">,
          signal,
        ),
      },
    ];
  }

  #validateTranslations(
    translations: readonly { blockId: string; text: string }[],
  ): void {
    if (!Array.isArray(translations) || translations.length === 0) {
      throw new TypeError("translations must not be empty");
    }
    const targetIds = new Set(this.#targetBlocks.map((item) => item.id));
    const seen = new Set<string>();
    for (const translation of translations) {
      if (!targetIds.has(translation.blockId)) {
        throw new Error(`unknown target block: ${translation.blockId}`);
      }
      if (seen.has(translation.blockId)) {
        throw new Error(`duplicate target block: ${translation.blockId}`);
      }
      if (typeof translation.text !== "string" || translation.text.trim().length === 0) {
        throw new TypeError(`empty translation: ${translation.blockId}`);
      }
      seen.add(translation.blockId);
    }
  }
}
