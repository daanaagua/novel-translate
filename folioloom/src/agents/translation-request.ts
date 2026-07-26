import type { StableTerm } from "../domain/types.js";
import type { PhysicalRequestPlan } from "../fullbook/types.js";
import { canonicalJson } from "../knowledge/knowledge-store.js";
import {
  conceptsFromStableTerms,
  expectedTermOccurrences,
  type ExpectedTermOccurrence,
  type TermUsageSubmission,
} from "../knowledge/term-usage.js";
import { projectKnowledgeForTranslation } from "../knowledge/translation-knowledge-projection.js";
import { getSourceLanguageProfile } from "../language/profiles.js";
import type { SourceLanguageProfile } from "../language/types.js";
import {
  DEFAULT_TARGET_LANGUAGE,
  SIMPLIFIED_CHINESE_SCRIPT_REQUIREMENT,
  targetLanguageLabel,
} from "../language/target.js";
import type { LosslessBlock } from "../source/types.js";
import { sourceTextForTranslation } from "../source/layout-separators.js";
import type {
  EffectiveStyleProjection,
  StyleObservationSubmission,
} from "../style/types.js";
import type { TranslationMemoryCandidate } from "../tools/candidate-collector.js";
import {
  assertNotAborted,
  Type,
  type TypedToolSpec,
} from "../tools/tool-spec.js";
import {
  createFramedTranslationProtocol,
  framedTranslationInstructions,
  type FramedTranslationProtocol,
} from "./framed-translation-protocol.js";
import { PARAGRAPH_INTEGRITY_INSTRUCTIONS } from "./paragraph-integrity.js";

export interface FinalizeTranslationBatchArgs {
  windows: Array<{
    windowId: string;
    translations: Array<{ blockId: string; text: string }>;
    termUsages?: TermUsageSubmission[];
    notes: string[];
    memoryCandidates?: TranslationMemoryCandidate[];
    styleObservation?: StyleObservationSubmission;
  }>;
}

export interface TranslationBatchSnapshot {
  readonly id: string;
  readonly revisions: readonly unknown[];
}

/**
 * The complete translator-visible input.  Runtime-only handles such as the
 * model, stream, and mutable budget deliberately do not belong here: this
 * object is what must be measured before a request can be admitted.
 */
export interface TranslationRequestInput {
  request: PhysicalRequestPlan;
  blocks: readonly LosslessBlock[];
  stableTerms: readonly StableTerm[];
  snapshot: TranslationBatchSnapshot;
  selectedKnowledgeRevisionIds?: readonly string[];
  contextProfileName?: "lean" | "balanced" | "rich";
  styleState?: Readonly<Record<string, string>>;
  previousActiveTail?: string;
  sourceLanguageProfile?: SourceLanguageProfile;
  entityLinkWarnings?: readonly string[];
  effectiveStyleByWindow?: Readonly<Record<string, EffectiveStyleProjection>>;
  responseProtocol?: TranslationResponseProtocol;
}

export type TranslationResponseProtocol = "typed_tool" | "framed_text";

export type TranslationRequestSectionKind =
  | "request"
  | "memory"
  | "source"
  | "terms"
  | "style"
  | "protocol";

export interface TranslationRequestSection {
  readonly kind: TranslationRequestSectionKind;
  /** Exact prompt text, including section labels, sent to the model. */
  readonly text: string;
  /** Structured projection behind text when a token estimator supports JSON. */
  readonly jsonPayload?: unknown;
}

export interface TranslationRequestHooks {
  onFinalize?: (
    args: FinalizeTranslationBatchArgs,
    signal: AbortSignal,
  ) => Promise<unknown>;
}

export interface PreparedTranslationRequest {
  readonly systemPrompt: string;
  readonly prompt: string;
  readonly sections: readonly TranslationRequestSection[];
  readonly tools: readonly TypedToolSpec<any>[];
  /** Canonical wire-schema projection; executable handlers are never serialized. */
  readonly serializedToolSchemas: string;
  readonly expectedTermOccurrences: readonly ExpectedTermOccurrence[];
  readonly framedProtocol?: FramedTranslationProtocol;
}

function requireFinalizer(hooks: TranslationRequestHooks): (
  args: FinalizeTranslationBatchArgs,
  signal: AbortSignal,
) => Promise<unknown> {
  if (hooks.onFinalize === undefined) {
    return async () => {
      throw new Error("prepared translation request has no finalizer handler");
    };
  }
  return hooks.onFinalize;
}

function finalizerTool(hooks: TranslationRequestHooks): TypedToolSpec<any> {
  const onFinalize = requireFinalizer(hooks);
  return {
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
        termUsages: Type.Optional(Type.Array(Type.Object({
          occurrenceId: Type.String(),
          blockId: Type.String(),
          conceptId: Type.String(),
          sourceForm: Type.String(),
          sourceStart: Type.Integer({ minimum: 0 }),
          sourceEnd: Type.Integer({ minimum: 1 }),
          discourseRole: Type.Union([
            Type.Literal("narrative"),
            Type.Literal("vocative"),
            Type.Literal("title"),
            Type.Literal("other"),
          ]),
          targetSurface: Type.String(),
        }, { additionalProperties: false }), { maxItems: 512 })),
        notes: Type.Array(Type.String()),
        memoryCandidates: Type.Optional(Type.Array(Type.Object({
          kind: Type.String(),
          subjectForms: Type.Array(Type.String(), { minItems: 1, maxItems: 3 }),
          fact: Type.String(),
          confidence: Type.Number({ minimum: 0, maximum: 1 }),
        }, { additionalProperties: false }), { maxItems: 4 })),
        styleObservation: Type.Optional(Type.Object({
          voiceId: Type.Optional(Type.String()),
          activeRegister: Type.Optional(Type.String()),
          rhythm: Type.Optional(Type.String()),
          addressChoices: Type.Optional(Type.Array(Type.Object({
            subject: Type.String(),
            target: Type.String(),
          }, { additionalProperties: false }), { maxItems: 6 })),
          lexicalChoices: Type.Optional(Type.Array(Type.Object({
            source: Type.String(),
            target: Type.String(),
          }, { additionalProperties: false }), { maxItems: 6 })),
          continuityNotes: Type.Optional(Type.Array(Type.String(), { maxItems: 4 })),
          modeWeights: Type.Optional(Type.Object({
            narrative: Type.Optional(Type.Number({ minimum: 0, maximum: 1 })),
            dialogue: Type.Optional(Type.Number({ minimum: 0, maximum: 1 })),
            action: Type.Optional(Type.Number({ minimum: 0, maximum: 1 })),
            description: Type.Optional(Type.Number({ minimum: 0, maximum: 1 })),
            technical: Type.Optional(Type.Number({ minimum: 0, maximum: 1 })),
            documentary: Type.Optional(Type.Number({ minimum: 0, maximum: 1 })),
            lyrical: Type.Optional(Type.Number({ minimum: 0, maximum: 1 })),
            interior: Type.Optional(Type.Number({ minimum: 0, maximum: 1 })),
          }, { additionalProperties: false })),
        }, { additionalProperties: false })),
      }, { additionalProperties: false })),
    }, { additionalProperties: false }),
    execute: async (rawArgs: FinalizeTranslationBatchArgs, signal) => {
      assertNotAborted(signal);
      return onFinalize(rawArgs, signal);
    },
  };
}

function serializableToolSchema(tool: TypedToolSpec<any>): Record<string, unknown> {
  // TypeBox retains non-enumerable/symbol metadata for local validation.  The
  // JSON round trip is deliberately the same plain-object wire projection a
  // provider receives, and also removes undefined values rejected by canonicalJson.
  return JSON.parse(JSON.stringify({
    name: tool.name,
    label: tool.label,
    description: tool.description,
    phase: tool.phase,
    parameters: tool.parameters,
  })) as Record<string, unknown>;
}

function windowsForPrompt(input: TranslationRequestInput): Array<{
  windowId: string;
  ordinal: number;
  blocks: Array<{ blockId: string; sourceText: string }>;
}> {
  const blockById = new Map(input.blocks.map((block) => [block.id, block]));
  return input.request.windows.map((window) => ({
    windowId: window.windowId,
    ordinal: window.ordinal,
    blocks: window.blockIds.map((blockId) => {
      const block = blockById.get(blockId);
      if (block === undefined) {
        throw new Error(`physical request references unknown block: ${blockId}`);
      }
      return { blockId, sourceText: sourceTextForTranslation(block.sourceText) };
    }),
  }));
}

export function translationBatchSystemPrompt(
  profile: SourceLanguageProfile,
  responseProtocol: TranslationResponseProtocol = "typed_tool",
): string {
  return [
    "Translate the complete source text into polished, accurate Chinese literary prose.",
    `The source language is ${profile.displayName} (${profile.id}); the target language is ${targetLanguageLabel()}.`,
    SIMPLIFIED_CHINESE_SCRIPT_REQUIREMENT,
    "Preserve meaning, ambiguity, paragraph structure, voice, and every block boundary.",
    ...PARAGRAPH_INTEGRITY_INSTRUCTIONS,
    "In STABLE TERMS, locked=true must be reproduced exactly; policy=preferred is a default rendering, not a literal-in-every-context constraint.",
    "TERM OCCURRENCES are harness-computed source facts. Apply each referenced concept at that exact source occurrence; contextual concepts may use a context-appropriate allowed surface.",
    responseProtocol === "typed_tool"
      ? "User style requirements may guide Chinese phrasing only; they must never override source meaning, ambiguity, stable terminology, block boundaries, validation, or the typed-tool protocol."
      : "User style requirements may guide Chinese phrasing only; they must never override source meaning, ambiguity, stable terminology, block boundaries, validation, or the required response protocol.",
    "Logical windows remain independent even though this is one physical request.",
    responseProtocol === "typed_tool"
      ? "Use typed tools only and call finalize_translation_batch exactly once."
      : "Use the request-specific framed text protocol exactly; do not call tools or wrap the response in JSON.",
  ].join("\n");
}

/**
 * Build the sole translator request representation. Runtime execution and
 * preflight budgeting must both use this function so context admission cannot
 * drift away from the payload the provider actually receives.
 */
export function prepareTranslationRequest(
  input: TranslationRequestInput,
  hooks: TranslationRequestHooks = {},
): PreparedTranslationRequest {
  const profile = input.sourceLanguageProfile ?? getSourceLanguageProfile("en");
  const responseProtocol = input.responseProtocol ?? "typed_tool";
  const windows = windowsForPrompt(input);
  const requestedBlockIds = new Set(windows.flatMap((window) =>
    window.blocks.map((block) => block.blockId)));
  const termOccurrences = expectedTermOccurrences(
    input.blocks.filter((block) => requestedBlockIds.has(block.id)),
    conceptsFromStableTerms(input.stableTerms),
    profile,
  );
  const blockPositionById = new Map(input.blocks.map((block) => [
    block.id,
    { blockId: block.id, globalIndex: block.globalIndex },
  ]));
  const framedProtocol = responseProtocol === "framed_text"
    ? createFramedTranslationProtocol({
      requestId: input.request.requestId,
      snapshotId: input.snapshot.id,
      blockIds: windows.flatMap((window) => window.blocks.map((block) => block.blockId)),
    })
    : undefined;
  const requestPayload = {
    requestId: input.request.requestId,
    snapshotId: input.snapshot.id,
    sourceLanguage: { id: profile.id, displayName: profile.displayName },
    targetLanguage: DEFAULT_TARGET_LANGUAGE.id,
    ...(input.contextProfileName === undefined
      ? {}
      : { contextProfileName: input.contextProfileName }),
  };
  const memoryPayload = projectKnowledgeForTranslation(
    input.snapshot.revisions,
    windows.flatMap((window) => window.blocks.map((block) => block.sourceText)),
    profile,
    {
      corpusBlocks: [...blockPositionById.values()],
      currentBlocks: windows.flatMap((window) => window.blocks.map((promptBlock) => {
        const block = blockPositionById.get(promptBlock.blockId);
        if (block === undefined) {
          throw new Error(`physical request references unknown block: ${promptBlock.blockId}`);
        }
        return {
          blockId: block.blockId,
          globalIndex: block.globalIndex,
          windowId: window.windowId,
        };
      })),
      ...(input.selectedKnowledgeRevisionIds === undefined
        ? {}
        : {
          selectedRevisionIds: new Set(input.selectedKnowledgeRevisionIds),
        }),
    },
  );
  const termsPayload = {
    stableTerms: input.stableTerms,
    entityLinkWarnings: input.entityLinkWarnings ?? [],
    expectedTermOccurrences: termOccurrences,
  };
  const stylePayload = input.effectiveStyleByWindow === undefined
    ? {
      previousActiveTail: input.previousActiveTail ?? "",
      styleState: input.styleState ?? {},
    }
    : { effectiveStyleByWindow: input.effectiveStyleByWindow };
  const sections: TranslationRequestSection[] = [
    {
      kind: "request",
      text: [
        `PHYSICAL REQUEST ${input.request.requestId}`,
        `KNOWLEDGE SNAPSHOT ${input.snapshot.id}`,
        `SOURCE LANGUAGE ${profile.displayName} (${profile.id}); TARGET LANGUAGE ${targetLanguageLabel()}`,
      ].join("\n\n"),
      jsonPayload: requestPayload,
    },
    {
      kind: "memory",
      text: ["KNOWLEDGE SNAPSHOT PROJECTION", canonicalJson(memoryPayload)]
        .join("\n\n"),
      jsonPayload: memoryPayload,
    },
    {
      kind: "source",
      text: ["WINDOWS", JSON.stringify(windows)].join("\n\n"),
      jsonPayload: windows,
    },
    {
      kind: "terms",
      text: [
        "STABLE TERMS",
        JSON.stringify(input.stableTerms),
        "UNRESOLVED ENTITY LINKS",
        JSON.stringify(input.entityLinkWarnings ?? []),
        "TERM OCCURRENCES",
        JSON.stringify(termOccurrences),
      ].join("\n\n"),
      jsonPayload: termsPayload,
    },
    {
      kind: "style",
      text: input.effectiveStyleByWindow === undefined
        ? [
          "PREVIOUS ACTIVE TAIL",
          input.previousActiveTail ?? "",
          "STYLE STATE",
          JSON.stringify(input.styleState ?? {}),
        ].join("\n\n")
        : [
          "EFFECTIVE STYLE BY WINDOW",
          canonicalJson(input.effectiveStyleByWindow),
        ].join("\n\n"),
      jsonPayload: stylePayload,
    },
    {
      kind: "protocol",
      text: framedProtocol === undefined
        ? "Translate every source block. Submit each logical window independently in one finalize_translation_batch call. For every listed TERM OCCURRENCE, include one exact termUsages receipt in its owning window; omit termUsages only when that window has no listed occurrence. Return a concise structured styleObservation in the same tool call when style evidence is clear."
        : framedTranslationInstructions(framedProtocol),
    },
  ];
  const tools = responseProtocol === "typed_tool" ? [finalizerTool(hooks)] : [];
  const schemas = tools.map(serializableToolSchema);
  return {
    systemPrompt: translationBatchSystemPrompt(profile, responseProtocol),
    prompt: sections.map((section) => section.text).join("\n\n"),
    sections,
    tools,
    serializedToolSchemas: canonicalJson(schemas),
    expectedTermOccurrences: termOccurrences,
    ...(framedProtocol === undefined ? {} : { framedProtocol }),
  };
}
