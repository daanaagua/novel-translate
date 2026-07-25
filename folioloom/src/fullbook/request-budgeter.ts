import {
  prepareTranslationRequest,
  type TranslationRequestInput,
  type TranslationRequestSectionKind,
} from "../agents/translation-request.js";
import { getSourceLanguageProfile } from "../language/profiles.js";
import type { SourceLanguageProfile } from "../language/types.js";

/** Minimal interface deliberately shared with the future source token estimator. */
export interface RequestTokenEstimate {
  readonly tokens: number;
  readonly uncertainty?: number;
  readonly estimatorVersion?: string;
}

export interface RequestTokenEstimator {
  estimateText(
    text: string,
    profile: SourceLanguageProfile,
    options?: { readonly modelId?: string },
  ): RequestTokenEstimate;
  /** Optional while the source estimator is text-only; JSON falls back to its wire text. */
  estimateJson?(
    value: unknown,
    profile: SourceLanguageProfile,
    options?: { readonly modelId?: string },
  ): RequestTokenEstimate;
}

export interface RequestBudgetOptions {
  /** Calibration scope for the provider/model that will execute this request. */
  readonly modelId?: string;
  /** Provider context capacity reserved for this complete request. */
  readonly contextWindowTokens: number;
  /** Expected completion budget, including all translated source blocks. */
  readonly outputTokens: number;
  /** Explicit hidden-reasoning reserve for models that use it. */
  readonly reasoningReserveTokens: number;
  /** Conservative fixed margin for provider framing and estimator error. */
  readonly safetyMarginTokens: number;
}

export type RequestBudgetComponentKind =
  | "system"
  | "request"
  | TranslationRequestSectionKind
  | "tool_schemas";

export interface RequestBudgetComponent {
  readonly kind: RequestBudgetComponentKind;
  readonly tokens: number;
  readonly uncertaintyTokens: number;
  readonly estimatorVersion?: string;
}

export type RequestBudgetDecision = "accepted" | "split_request" | "split_window";

export interface RequestBudgetAssessment {
  readonly components: readonly RequestBudgetComponent[];
  readonly inputTokens: number;
  readonly inputUncertaintyTokens: number;
  readonly outputTokens: number;
  readonly reasoningReserveTokens: number;
  readonly safetyMarginTokens: number;
  readonly totalReserved: number;
  readonly contextWindowTokens: number;
  readonly fits: boolean;
  /** A deterministic capacity action; this module never mutates scheduling state. */
  readonly decision: RequestBudgetDecision;
}

function positiveInteger(value: number, label: string): number {
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new TypeError(`${label} must be a positive safe integer`);
  }
  return value;
}

function nonNegativeInteger(value: number, label: string): number {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new TypeError(`${label} must be a non-negative safe integer`);
  }
  return value;
}

function normalizeEstimate(value: RequestTokenEstimate, label: string): {
  tokens: number;
  uncertaintyTokens: number;
  estimatorVersion?: string;
} {
  if (value === null || typeof value !== "object") {
    throw new TypeError(`${label} must return an estimate object`);
  }
  const tokens = Math.ceil(value.tokens);
  const uncertaintyTokens = Math.ceil(value.uncertainty ?? 0);
  if (!Number.isSafeInteger(tokens) || tokens < 0) {
    throw new TypeError(`${label}.tokens must be a non-negative finite number`);
  }
  if (!Number.isSafeInteger(uncertaintyTokens) || uncertaintyTokens < 0) {
    throw new TypeError(`${label}.uncertainty must be a non-negative finite number`);
  }
  return {
    tokens,
    uncertaintyTokens,
    ...(value.estimatorVersion === undefined ? {} : { estimatorVersion: value.estimatorVersion }),
  };
}

function addSafely(left: number, right: number, label: string): number {
  const result = left + right;
  if (!Number.isSafeInteger(result)) {
    throw new RangeError(`${label} exceeds safe integer range`);
  }
  return result;
}

/**
 * Admission control for the exact prompt/tool payload that runtime will send.
 * It measures every text section and, when available, the corresponding JSON
 * projection.  Taking the higher estimate guards against optimistic estimators
 * without double counting the same serialized material.
 */
export class RequestBudgeter {
  readonly #estimator: RequestTokenEstimator;
  readonly #options: RequestBudgetOptions;

  constructor(estimator: RequestTokenEstimator, options: RequestBudgetOptions) {
    this.#estimator = estimator;
    this.#options = {
      ...(options.modelId === undefined ? {} : { modelId: options.modelId }),
      contextWindowTokens: positiveInteger(options.contextWindowTokens, "contextWindowTokens"),
      outputTokens: nonNegativeInteger(options.outputTokens, "outputTokens"),
      reasoningReserveTokens: nonNegativeInteger(
        options.reasoningReserveTokens,
        "reasoningReserveTokens",
      ),
      safetyMarginTokens: nonNegativeInteger(options.safetyMarginTokens, "safetyMarginTokens"),
    };
  }

  assess(input: TranslationRequestInput): RequestBudgetAssessment {
    const prepared = prepareTranslationRequest(input);
    const profile = input.sourceLanguageProfile ?? getSourceLanguageProfile("en");
    const components: RequestBudgetComponent[] = [];
    const measure = (
      kind: RequestBudgetComponentKind,
      text: string,
      jsonPayload?: unknown,
    ): void => {
      const textual = normalizeEstimate(
        this.#estimator.estimateText(text, profile, { modelId: this.#options.modelId }),
        `estimateText(${kind})`,
      );
      const structured = jsonPayload === undefined
        ? undefined
        : this.#estimator.estimateJson === undefined
          ? normalizeEstimate(
            this.#estimator.estimateText(
              JSON.stringify(jsonPayload) ?? "null",
              profile,
              { modelId: this.#options.modelId },
            ),
            `estimateText(json:${kind})`,
          )
          : normalizeEstimate(
            this.#estimator.estimateJson(
              jsonPayload,
              profile,
              { modelId: this.#options.modelId },
            ),
            `estimateJson(${kind})`,
          );
      components.push({
        kind,
        tokens: Math.max(textual.tokens, structured?.tokens ?? 0),
        uncertaintyTokens: Math.max(
          textual.uncertaintyTokens,
          structured?.uncertaintyTokens ?? 0,
        ),
        ...(textual.estimatorVersion === undefined && structured?.estimatorVersion === undefined
          ? {}
          : { estimatorVersion: textual.estimatorVersion ?? structured?.estimatorVersion }),
      });
    };

    measure("system", prepared.systemPrompt);
    for (const section of prepared.sections) {
      measure(section.kind, section.text, section.jsonPayload);
    }
    const toolSchemaPayload = JSON.parse(prepared.serializedToolSchemas) as unknown;
    measure("tool_schemas", prepared.serializedToolSchemas, toolSchemaPayload);

    const inputTokens = components.reduce(
      (total, component) => addSafely(total, component.tokens, "input token total"),
      0,
    );
    const inputUncertaintyTokens = components.reduce(
      (total, component) => addSafely(
        total,
        component.uncertaintyTokens,
        "input uncertainty total",
      ),
      0,
    );
    const totalReserved = [
      inputTokens,
      inputUncertaintyTokens,
      this.#options.outputTokens,
      this.#options.reasoningReserveTokens,
      this.#options.safetyMarginTokens,
    ].reduce((total, value) => addSafely(total, value, "request token reservation"), 0);
    const fits = totalReserved <= this.#options.contextWindowTokens;
    const decision: RequestBudgetDecision = fits
      ? "accepted"
      : input.request.windows.length > 1
        ? "split_request"
        : "split_window";
    return {
      components,
      inputTokens,
      inputUncertaintyTokens,
      outputTokens: this.#options.outputTokens,
      reasoningReserveTokens: this.#options.reasoningReserveTokens,
      safetyMarginTokens: this.#options.safetyMarginTokens,
      totalReserved,
      contextWindowTokens: this.#options.contextWindowTokens,
      fits,
      decision,
    };
  }
}
