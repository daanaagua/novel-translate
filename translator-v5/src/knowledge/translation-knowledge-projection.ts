import { Buffer } from "node:buffer";

import {
  canonicalJson,
  type KnowledgeStatus,
} from "./knowledge-store.js";
import { sourceFormsFromRevision } from "./knowledge-source-forms.js";
import type { SourceLanguageProfile } from "../language/types.js";

/**
 * Translator prompts must not grow with the durable book snapshot.  These
 * limits apply only to the wire projection; the persistent snapshot remains
 * complete and keeps its original identity.
 */
export const DEFAULT_TRANSLATION_KNOWLEDGE_MAX_ENTRIES = 24;
export const DEFAULT_TRANSLATION_KNOWLEDGE_MAX_BYTES = 24_000;
export const DEFAULT_TRANSLATION_KNOWLEDGE_GLOBAL_FALLBACK_ENTRIES = 2;

const TRANSLATOR_VISIBLE_STATUSES = new Set<KnowledgeStatus>([
  "provisional",
  "active",
  "needs_revalidate",
  "contextual",
]);

const GLOBAL_FALLBACK_KINDS = new Set([
  "lexical_anchor",
  "term_sense",
]);

export interface TranslationKnowledgeProjectionOptions {
  /** Maximum number of individual knowledge revisions exposed to one request. */
  readonly maxEntries?: number;
  /** Maximum UTF-8 bytes of the canonical wire payload, metadata included. */
  readonly maxSerializedBytes?: number;
  /** Reserved deterministic fallbacks for global terminology/revalidation facts. */
  readonly maxGlobalFallbackEntries?: number;
}

export interface TranslationKnowledgeProjectionMetadata {
  readonly total: number;
  readonly projected: number;
  readonly omitted: number;
  readonly maxEntries: number;
  readonly maxSerializedBytes: number;
  readonly serializedBytes: number;
}

export interface ProjectedKnowledgeRevision {
  readonly revisionId: string;
  readonly revision: number;
  readonly normalizedSubject: string;
  readonly kind: string;
  readonly status: Extract<KnowledgeStatus,
    "provisional" | "active" | "needs_revalidate" | "contextual">;
  readonly scope: "source_matched" | "global_fallback";
  readonly payload: unknown;
  readonly alternatives: readonly unknown[];
}

/**
 * This is the only knowledge structure serialized into a translator prompt.
 * It contains no pointer to the full snapshot or omitted revision contents.
 */
export interface TranslationKnowledgeProjection {
  readonly schemaVersion: "v5-translation-knowledge-projection-1";
  readonly metadata: TranslationKnowledgeProjectionMetadata;
  readonly revisions: readonly ProjectedKnowledgeRevision[];
}

interface ParsedRevision {
  readonly revisionId: string;
  readonly revision: number;
  readonly normalizedSubject: string;
  readonly kind: string;
  readonly status: Extract<KnowledgeStatus,
    "provisional" | "active" | "needs_revalidate" | "contextual">;
  readonly payload: unknown;
  readonly alternatives: readonly unknown[];
}

interface Candidate {
  readonly revision: ParsedRevision;
  readonly scope: ProjectedKnowledgeRevision["scope"];
}

interface ResolvedOptions {
  readonly maxEntries: number;
  readonly maxSerializedBytes: number;
  readonly maxGlobalFallbackEntries: number;
}

function compareText(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

function nonNegativeSafeInteger(value: number, label: string): number {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new TypeError(`${label} must be a non-negative safe integer`);
  }
  return value;
}

function resolvedOptions(
  options: TranslationKnowledgeProjectionOptions,
): ResolvedOptions {
  const maxEntries = nonNegativeSafeInteger(
    options.maxEntries ?? DEFAULT_TRANSLATION_KNOWLEDGE_MAX_ENTRIES,
    "maxEntries",
  );
  const maxSerializedBytes = nonNegativeSafeInteger(
    options.maxSerializedBytes ?? DEFAULT_TRANSLATION_KNOWLEDGE_MAX_BYTES,
    "maxSerializedBytes",
  );
  const maxGlobalFallbackEntries = nonNegativeSafeInteger(
    options.maxGlobalFallbackEntries
      ?? DEFAULT_TRANSLATION_KNOWLEDGE_GLOBAL_FALLBACK_ENTRIES,
    "maxGlobalFallbackEntries",
  );
  return {
    maxEntries,
    maxSerializedBytes,
    maxGlobalFallbackEntries: Math.min(maxEntries, maxGlobalFallbackEntries),
  };
}

function record(value: unknown): Readonly<Record<string, unknown>> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Readonly<Record<string, unknown>>
    : undefined;
}

function nonemptyString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim().length > 0 ? value : undefined;
}

function asVisibleStatus(value: unknown): ParsedRevision["status"] | undefined {
  return typeof value === "string" && TRANSLATOR_VISIBLE_STATUSES.has(value as KnowledgeStatus)
    ? value as ParsedRevision["status"]
    : undefined;
}

function parseRevision(value: unknown): ParsedRevision | undefined {
  const raw = record(value);
  if (raw === undefined) return undefined;
  const revisionId = nonemptyString(raw.revisionId);
  const normalizedSubject = nonemptyString(raw.normalizedSubject);
  const kind = nonemptyString(raw.kind);
  const status = asVisibleStatus(raw.status);
  if (revisionId === undefined
    || normalizedSubject === undefined
    || kind === undefined
    || status === undefined
    || !Number.isSafeInteger(raw.revision)
    || (raw.revision as number) < 1
    || !Array.isArray(raw.alternatives)
    || !("payload" in raw)) {
    return undefined;
  }
  return {
    revisionId,
    revision: raw.revision as number,
    normalizedSubject,
    kind,
    status,
    payload: raw.payload,
    alternatives: raw.alternatives,
  };
}

function formsForRevision(revision: ParsedRevision): string[] {
  const forms = new Set(sourceFormsFromRevision(revision));
  // Ordinary durable memory candidates normalize their first subject form into
  // normalizedSubject.  Entity-link keys use an internal prefix, so payload
  // forms above remain the matching route for those records.
  if (!revision.normalizedSubject.startsWith("entity-alias:")) {
    forms.add(revision.normalizedSubject);
  }
  return [...forms].sort(compareText);
}

function identifierCharacter(value: string | undefined): boolean {
  return value !== undefined && /[\p{L}\p{N}]/u.test(value);
}

function containsWordOrPhrase(
  source: string,
  form: string,
  profile: SourceLanguageProfile,
): boolean {
  let start = source.indexOf(form);
  while (start >= 0) {
    const before = source.at(start - 1);
    const after = source.at(start + form.length);
    const cjk = profile.scripts.some((script) =>
      script === "kana" || script === "hangul" || script === "han");
    if (cjk || (!identifierCharacter(before) && !identifierCharacter(after))) {
      return true;
    }
    start = source.indexOf(form, start + form.length);
  }
  return false;
}

function sourceMatchesRevision(
  revision: ParsedRevision,
  sourceText: string,
  sourceTokens: ReadonlySet<string>,
  profile: SourceLanguageProfile,
): boolean {
  for (const rawForm of formsForRevision(revision)) {
    const form = profile.normalizeSourceForm(rawForm);
    // One-scalar labels create high false-positive rates (notably Latin "I");
    // they remain available through stable terminology when explicitly locked.
    if (form.length < 2) continue;
    if (sourceTokens.has(form) || containsWordOrPhrase(sourceText, form, profile)) {
      return true;
    }
  }
  return false;
}

function isGlobalFallback(revision: ParsedRevision): boolean {
  return revision.status === "needs_revalidate" || GLOBAL_FALLBACK_KINDS.has(revision.kind);
}

function compareCandidate(left: Candidate, right: Candidate): number {
  return compareText(left.revision.normalizedSubject, right.revision.normalizedSubject)
    || compareText(left.revision.kind, right.revision.kind)
    || left.revision.revision - right.revision.revision
    || compareText(left.revision.revisionId, right.revision.revisionId);
}

function projectedRevision(candidate: Candidate): ProjectedKnowledgeRevision {
  return {
    revisionId: candidate.revision.revisionId,
    revision: candidate.revision.revision,
    normalizedSubject: candidate.revision.normalizedSubject,
    kind: candidate.revision.kind,
    status: candidate.revision.status,
    scope: candidate.scope,
    payload: candidate.revision.payload,
    alternatives: candidate.revision.alternatives,
  };
}

function projectionWithStableByteCount(
  total: number,
  revisions: readonly ProjectedKnowledgeRevision[],
  options: ResolvedOptions,
): TranslationKnowledgeProjection {
  let serializedBytes = 0;
  for (let attempt = 0; attempt < 12; attempt += 1) {
    const projection: TranslationKnowledgeProjection = {
      schemaVersion: "v5-translation-knowledge-projection-1",
      metadata: {
        total,
        projected: revisions.length,
        omitted: total - revisions.length,
        maxEntries: options.maxEntries,
        maxSerializedBytes: options.maxSerializedBytes,
        serializedBytes,
      },
      revisions,
    };
    const measured = Buffer.byteLength(canonicalJson(projection), "utf8");
    if (measured === serializedBytes) return projection;
    serializedBytes = measured;
  }
  throw new Error("knowledge projection byte count did not converge");
}

function appendIfWithinBounds(
  total: number,
  selected: ProjectedKnowledgeRevision[],
  candidate: Candidate,
  options: ResolvedOptions,
): boolean {
  if (selected.length >= options.maxEntries) return false;
  const next = [...selected, projectedRevision(candidate)];
  let projection: TranslationKnowledgeProjection;
  try {
    projection = projectionWithStableByteCount(total, next, options);
  } catch {
    // A malformed legacy record is safer omitted than copied wholesale to a
    // prompt; durable snapshots are never mutated by this decision.
    return false;
  }
  if (projection.metadata.serializedBytes > options.maxSerializedBytes) return false;
  selected.push(projectedRevision(candidate));
  return true;
}

/**
 * Produce a deterministic, request-local view of durable knowledge.  The
 * caller's complete snapshot is neither modified nor serialized wholesale.
 */
export function projectKnowledgeForTranslation(
  revisions: readonly unknown[],
  sourceTexts: readonly string[],
  profile: SourceLanguageProfile,
  options: TranslationKnowledgeProjectionOptions = {},
): TranslationKnowledgeProjection {
  if (!Array.isArray(revisions)) {
    throw new TypeError("revisions must be an array");
  }
  if (!Array.isArray(sourceTexts) || sourceTexts.some((text) => typeof text !== "string")) {
    throw new TypeError("sourceTexts must be an array of strings");
  }
  const resolved = resolvedOptions(options);
  const total = revisions.length;
  const normalizedSource = profile.normalizeSourceForm(sourceTexts.join("\n"));
  const sourceTokens = new Set(profile.segment(sourceTexts.join("\n"))
    .filter((token) => token.isWordLike)
    .map((token) => token.normalized));
  const matchingNeedsRevalidate: Candidate[] = [];
  const matchingOther: Candidate[] = [];
  const globalFallbacks: Candidate[] = [];

  for (const raw of revisions) {
    const revision = parseRevision(raw);
    if (revision === undefined) continue;
    const matched = sourceMatchesRevision(revision, normalizedSource, sourceTokens, profile);
    if (matched) {
      const candidate: Candidate = { revision, scope: "source_matched" };
      if (revision.status === "needs_revalidate") {
        matchingNeedsRevalidate.push(candidate);
      } else {
        matchingOther.push(candidate);
      }
      continue;
    }
    // Contextual facts must have literal current-source evidence.  Other
    // records can supply only the deliberately tiny, deterministic fallback.
    if (revision.status !== "contextual" && isGlobalFallback(revision)) {
      globalFallbacks.push({ revision, scope: "global_fallback" });
    }
  }

  matchingNeedsRevalidate.sort(compareCandidate);
  matchingOther.sort(compareCandidate);
  globalFallbacks.sort(compareCandidate);

  const empty = projectionWithStableByteCount(total, [], resolved);
  if (empty.metadata.serializedBytes > resolved.maxSerializedBytes) {
    throw new RangeError("maxSerializedBytes cannot fit knowledge projection metadata");
  }

  const selected: ProjectedKnowledgeRevision[] = [];
  for (const candidate of matchingNeedsRevalidate) {
    appendIfWithinBounds(total, selected, candidate, resolved);
  }

  // Literal evidence in the current source always outranks a global fallback.
  // Otherwise two unrelated terminology fallbacks could evict an identity or
  // causal fact that the current passage actually names.
  for (const candidate of matchingOther) {
    appendIfWithinBounds(total, selected, candidate, resolved);
  }

  let fallbackCount = 0;
  for (const candidate of globalFallbacks) {
    if (fallbackCount >= resolved.maxGlobalFallbackEntries) break;
    if (appendIfWithinBounds(total, selected, candidate, resolved)) fallbackCount += 1;
  }

  const projection = projectionWithStableByteCount(total, selected, resolved);
  if (projection.metadata.serializedBytes > resolved.maxSerializedBytes) {
    throw new Error("knowledge projection exceeds its canonical byte budget");
  }
  return projection;
}
