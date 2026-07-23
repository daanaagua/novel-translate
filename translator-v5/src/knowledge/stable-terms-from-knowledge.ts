import { entityLinkAsTerms, type EntityLink } from "../domain/entity-links.js";
import type { StableTerm } from "../domain/types.js";
import type { KnowledgeRevision } from "./knowledge-store.js";

function record(value: unknown): Readonly<Record<string, unknown>> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Readonly<Record<string, unknown>>
    : undefined;
}

function modelSafeTerm(term: StableTerm): StableTerm {
  if (!term.conceptId.startsWith("run-anchor-")
    || (term.locked && term.policy === "locked")) {
    return { ...term };
  }
  return {
    ...term,
    locked: false,
    policy: "preferred",
    note: "single-pass model anchor; prefer this rendering but allow context-sensitive Chinese wording",
  };
}

/**
 * Convert active durable lexical knowledge into the stable-term wire protocol.
 * User/import policy remains authoritative; single-pass model anchors are
 * softened to preferences unless the existing evidence protocol locked them.
 */
export function stableTermsFromKnowledge(
  revisions: readonly unknown[],
): StableTerm[] {
  const terms: StableTerm[] = [];
  for (const raw of revisions) {
    if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
      continue;
    }
    const revision = raw as Partial<KnowledgeRevision>;
    if (revision.status !== "active") continue;
    const payload = record(revision.payload);
    if (revision.kind === "lexical_anchor" && payload !== undefined) {
      if (typeof payload.sourceForm === "string"
        && typeof payload.canonicalSource === "string"
        && typeof payload.target === "string"
        && typeof payload.locked === "boolean") {
        const term: StableTerm = {
          conceptId: typeof payload.conceptId === "string"
            ? payload.conceptId
            : `user-${revision.normalizedSubject ?? payload.canonicalSource}`,
          lexemeId: typeof payload.lexemeId === "string"
            ? payload.lexemeId
            : `user-${revision.revisionId ?? revision.normalizedSubject ?? payload.sourceForm}`,
          sourceForm: payload.sourceForm,
          canonicalSource: payload.canonicalSource,
          target: payload.target,
          locked: payload.locked,
          ...(payload.policy === undefined
            ? {}
            : { policy: payload.policy as StableTerm["policy"] }),
          ...(payload.note === undefined ? {} : { note: String(payload.note) }),
          origin: "knowledge",
        };
        const modelAuthored = revision.authority === undefined
          || revision.authority.origin === "model";
        terms.push(modelAuthored ? modelSafeTerm(term) : term);
      }
      continue;
    }
    if (revision.kind === "entity_alias_link" && payload !== undefined) {
      terms.push(...entityLinkAsTerms(payload as unknown as EntityLink).map((term) => ({
        ...term,
        origin: term.origin ?? "knowledge",
      })));
    }
  }
  return terms;
}
