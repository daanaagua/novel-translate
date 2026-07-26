import { createHash } from "node:crypto";

import type { ConceptOccurrence } from "./concept-occurrence-index.js";
import type { LexicalConcept } from "./lexical-concept.js";
import {
  termSurfaceAllowed,
  type TermUsageSubmission,
} from "./term-usage.js";

export interface TranslationConceptDependency {
  readonly conceptId: string;
  readonly appliedRevisionId: string;
  readonly appliedRenderFingerprint: string;
}

export interface ActiveTranslationDependency {
  readonly translationId: number;
  readonly blockId: string;
  readonly snapshotId: string;
  readonly bindings: readonly TranslationConceptDependency[];
}

export interface RevalidationCandidate {
  readonly translationId: number;
  readonly blockId: string;
  readonly fromSnapshotId: string;
  readonly toSnapshotId: string;
  readonly conceptIds: readonly string[];
  readonly changeSetHash: string;
}

export interface StagedConceptBindingDependency
  extends TranslationConceptDependency {
  readonly termUsages: readonly TermUsageSubmission[];
}

export interface BindingRevisionUpdate {
  readonly conceptId: string;
  readonly revisionId: string;
  readonly renderFingerprint: string;
}

export interface BindingGateDecision {
  readonly status: "compatible" | "retry_latest_snapshot";
  readonly updates: readonly BindingRevisionUpdate[];
  readonly incompatibleConceptIds: readonly string[];
}

export interface SparseRevalidationInput {
  readonly concepts: readonly LexicalConcept[];
  readonly occurrences: readonly ConceptOccurrence[];
  readonly translations: readonly ActiveTranslationDependency[];
  readonly toSnapshotId: string;
}

function changeSetHash(
  concepts: readonly LexicalConcept[],
): string {
  return createHash("sha256")
    .update(JSON.stringify(concepts.map((concept) => ({
      conceptId: concept.conceptId,
      revisionId: concept.revisionId,
      renderFingerprint: concept.renderFingerprint,
    }))), "utf8")
    .digest("hex");
}

/**
 * Intersect changed rendering policies with exact source occurrences and the
 * bindings of active translation versions. No source text or full snapshot is
 * copied into the resulting task list.
 */
export function planSparseRevalidation(
  input: SparseRevalidationInput,
): RevalidationCandidate[] {
  if (typeof input.toSnapshotId !== "string"
    || input.toSnapshotId.trim().length === 0) {
    throw new TypeError("toSnapshotId must be nonempty");
  }
  const conceptById = new Map<string, LexicalConcept>();
  for (const concept of input.concepts) {
    const previous = conceptById.get(concept.conceptId);
    if (previous !== undefined
      && (previous.revisionId !== concept.revisionId
        || previous.renderFingerprint !== concept.renderFingerprint)) {
      throw new Error(`conflicting current concept ${concept.conceptId}`);
    }
    conceptById.set(concept.conceptId, concept);
  }
  const occurrenceKeys = new Set(input.occurrences.map((occurrence) =>
    `${occurrence.conceptId}\0${occurrence.blockId}`));
  const candidates: RevalidationCandidate[] = [];
  for (const translation of input.translations) {
    if (!Number.isSafeInteger(translation.translationId)
      || translation.translationId < 1) {
      throw new TypeError("translationId must be a positive safe integer");
    }
    const changed = new Map<string, LexicalConcept>();
    for (const binding of translation.bindings) {
      const current = conceptById.get(binding.conceptId);
      if (current === undefined
        || !occurrenceKeys.has(`${binding.conceptId}\0${translation.blockId}`)
        || binding.appliedRevisionId === current.revisionId
        || binding.appliedRenderFingerprint === current.renderFingerprint) {
        continue;
      }
      changed.set(current.conceptId, current);
    }
    const concepts = [...changed.values()].sort((left, right) =>
      left.conceptId.localeCompare(right.conceptId));
    if (concepts.length === 0) continue;
    candidates.push({
      translationId: translation.translationId,
      blockId: translation.blockId,
      fromSnapshotId: translation.snapshotId,
      toSnapshotId: input.toSnapshotId,
      conceptIds: concepts.map((concept) => concept.conceptId),
      changeSetHash: changeSetHash(concepts),
    });
  }
  return candidates.sort((left, right) =>
    left.translationId - right.translationId
    || left.blockId.localeCompare(right.blockId));
}

/**
 * Recheck only the surfaces recorded by the staged translation. A revision
 * can be adopted locally when every submitted surface remains legal; missing
 * receipts or a disallowed surface require a fresh translation snapshot.
 */
export function evaluateStagedConceptBindings(input: {
  readonly concepts: readonly LexicalConcept[];
  readonly bindings: readonly StagedConceptBindingDependency[];
}): BindingGateDecision {
  const conceptById = new Map(input.concepts.map((concept) => [
    concept.conceptId,
    concept,
  ]));
  const incompatible = new Set<string>();
  const updates = new Map<string, BindingRevisionUpdate>();
  for (const binding of input.bindings) {
    const current = conceptById.get(binding.conceptId);
    if (current === undefined || binding.appliedRevisionId === current.revisionId) {
      continue;
    }
    const allowed = binding.appliedRenderFingerprint === current.renderFingerprint
      || (binding.termUsages.length > 0
        && binding.termUsages.every((usage) =>
          usage.conceptId === binding.conceptId
          && termSurfaceAllowed(current, usage.targetSurface)));
    if (!allowed) {
      incompatible.add(binding.conceptId);
      continue;
    }
    updates.set(binding.conceptId, {
      conceptId: binding.conceptId,
      revisionId: current.revisionId,
      renderFingerprint: current.renderFingerprint,
    });
  }
  const incompatibleConceptIds = [...incompatible].sort();
  return {
    status: incompatibleConceptIds.length === 0
      ? "compatible"
      : "retry_latest_snapshot",
    updates: [...updates.values()].sort((left, right) =>
      left.conceptId.localeCompare(right.conceptId)),
    incompatibleConceptIds,
  };
}
