import type { V4Block } from "../domain/types.js";
import { getSourceLanguageProfile } from "../language/profiles.js";
import type { SourceLanguageProfile } from "../language/types.js";
import { normalizeSourceSceneSeparators } from "../source/layout-separators.js";
import {
  alignmentFingerprint,
  hanGraphemeLength,
  hasSemanticText,
  hasInvalidUnicodeScalar,
  letterGraphemeLength,
  semanticCharacterLength,
} from "../text/semantic-text.js";
import type { TranslationCandidate } from "../tools/candidate-collector.js";
import type { ValidationFailure } from "../tools/repair-tools.js";

export interface TranslationValidation {
  valid: boolean;
  failures: ValidationFailure[];
}

export interface TranslationValidationPolicy {
  allowedLatinTokens?: readonly string[];
  requiredTerms?: readonly { sourceForm: string; target: string }[];
  sourceLanguageProfile?: SourceLanguageProfile;
}

function paragraphCount(text: string): number {
  return normalizeSourceSceneSeparators(text)
    .split(/(?:\r?\n)[\t ]*(?:\r?\n)+/u)
    .filter(hasSemanticText)
    .length;
}

function meaningfulLength(text: string): number {
  return semanticCharacterLength(text);
}

const MIN_CROSS_BLOCK_PARAGRAPH_CHARACTERS = 48;

function normalizedLongParagraphs(text: string): Set<string> {
  return new Set(normalizeSourceSceneSeparators(text)
    .split(/(?:\r?\n)[\t ]*(?:\r?\n)+/u)
    .map(alignmentFingerprint)
    .filter((paragraph) => (
      [...paragraph].length >= MIN_CROSS_BLOCK_PARAGRAPH_CHARACTERS
    )));
}

const SYSTEM_LEAK_PATTERNS = [
  /["']systemPrompt["']\s*:/iu,
  /["']reasoning_content["']\s*:/iu,
  /<\/?tool_call>/iu,
  /["']toolCallId["']\s*:/iu,
  /\{\s*["']translations["']\s*:/iu,
];
const SOURCE_LAYOUT_TOKEN_PATTERN = /(?:\[[ \t]*\[[ \t]*\][ \t]*\]|［[ \t]*［[ \t]*］[ \t]*］)/u;

function isolatedSourceIdentifiers(sourceText: string): string[] {
  return sourceText.split(/\r?\n/u).flatMap((line) => {
    const value = line.trim();
    if (!/^[\p{L}\p{N}_@.+-]{2,64}$/u.test(value)) {
      return [];
    }
    const looksIdentifierLike = /\p{Ll}\p{Lu}/u.test(value)
      || /[\p{N}_@.+-]/u.test(value);
    return looksIdentifierLike ? [value] : [];
  });
}

export class TranslationValidator {
  validateCrossBlockAlignment(
    blocks: readonly V4Block[],
    candidate: TranslationCandidate,
  ): TranslationValidation {
    const sourceParagraphs = new Map(blocks.map((block) => [
      block.id,
      normalizedLongParagraphs(block.sourceText),
    ]));
    const targetParagraphs = new Map(candidate.translations.map((translation) => [
      translation.blockId,
      normalizedLongParagraphs(translation.text),
    ]));
    const signatureCounts = (
      paragraphsByBlock: ReadonlyMap<string, Set<string>>,
    ): Map<string, { blockIds: string[]; count: number }> => {
      const blockIdsByParagraph = new Map<string, string[]>();
      for (const [blockId, paragraphs] of paragraphsByBlock) {
        for (const paragraph of paragraphs) {
          const blockIds = blockIdsByParagraph.get(paragraph) ?? [];
          blockIds.push(blockId);
          blockIdsByParagraph.set(paragraph, blockIds);
        }
      }
      const counts = new Map<string, { blockIds: string[]; count: number }>();
      for (const blockIds of blockIdsByParagraph.values()) {
        const ordered = [...new Set(blockIds)].sort();
        if (ordered.length < 2) {
          continue;
        }
        const signature = ordered.join("\u0000");
        const previous = counts.get(signature);
        counts.set(signature, {
          blockIds: ordered,
          count: (previous?.count ?? 0) + 1,
        });
      }
      return counts;
    };
    const sourceSignatures = signatureCounts(sourceParagraphs);
    const targetSignatures = signatureCounts(targetParagraphs);
    const overlaps = new Map<string, { groups: number; maximumGroupSize: number }>();
    for (const [signature, target] of targetSignatures) {
      const groundedCount = sourceSignatures.get(signature)?.count ?? 0;
      if (target.count <= groundedCount) {
        continue;
      }
      for (const blockId of target.blockIds) {
        const previous = overlaps.get(blockId);
        overlaps.set(blockId, {
          groups: (previous?.groups ?? 0) + (target.count - groundedCount),
          maximumGroupSize: Math.max(previous?.maximumGroupSize ?? 0, target.blockIds.length),
        });
      }
    }
    const failures = [...overlaps.entries()].map(([blockId, summary]): ValidationFailure => ({
      code: "cross_block_translation_overlap",
      blockId,
      message: `target contains ${summary.groups} ungrounded repeated long paragraph group(s) spanning up to ${summary.maximumGroupSize} blocks`,
      repairable: true,
    }));
    return { valid: failures.length === 0, failures };
  }

  validate(
    blocks: readonly V4Block[],
    candidate: TranslationCandidate,
    policy: TranslationValidationPolicy = {},
  ): TranslationValidation {
    const failures: ValidationFailure[] = [];
    const expectedIds = blocks.map((block) => block.id);
    const actualIds = candidate.translations.map((item) => item.blockId);
    const actualSet = new Set(actualIds);
    const duplicates = actualIds.filter((id, index) => actualIds.indexOf(id) !== index);
    const missing = expectedIds.filter((id) => !actualSet.has(id));
    const extra = [...actualSet].filter((id) => !expectedIds.includes(id));
    if (duplicates.length > 0 || missing.length > 0 || extra.length > 0) {
      failures.push({
        code: "block_set_mismatch",
        message: `expected exact block set; missing=${missing.join(",") || "none"}; extra=${extra.join(",") || "none"}; duplicates=${[...new Set(duplicates)].join(",") || "none"}`,
        repairable: true,
      });
    }

    const sourceText = blocks.map((block) => block.sourceText).join("\n");
    const targetText = candidate.translations.map((item) => item.text).join("\n");
    const sourceClosingExcess = Math.max(
      0,
      [...sourceText.matchAll(/”/gu)].length - [...sourceText.matchAll(/“/gu)].length,
    );
    const targetClosingExcess = Math.max(
      0,
      [...targetText.matchAll(/”/gu)].length - [...targetText.matchAll(/“/gu)].length,
    );
    if (targetClosingExcess > sourceClosingExcess) {
      failures.push({
        code: "quote_boundary_mismatch",
        message: `target has ${targetClosingExcess} excess closing double quotes; source boundary allowance is ${sourceClosingExcess}`,
        repairable: true,
      });
    }
    if (/["‛‟〝〞„]/u.test(targetText)) {
      failures.push({
        code: "nonstandard_quote_glyph",
        message: "translation contains nonstandard double-quote glyphs",
        repairable: true,
      });
    }

    const blockById = new Map(blocks.map((block) => [block.id, block]));
    const profile = policy.sourceLanguageProfile ?? getSourceLanguageProfile("en");
    let sourceLength = 0;
    let targetLength = 0;
    for (const translation of candidate.translations) {
      const source = blockById.get(translation.blockId);
      if (typeof translation.text !== "string" || !hasSemanticText(translation.text)) {
        failures.push({
          code: "empty_translation",
          blockId: translation.blockId,
          message: "translation is empty",
          repairable: true,
        });
        continue;
      }
      if (SYSTEM_LEAK_PATTERNS.some((pattern) => pattern.test(translation.text))) {
        failures.push({
          code: "system_json_leak",
          blockId: translation.blockId,
          message: "translation contains system/tool protocol material",
          repairable: true,
        });
      }
      const untranslated = profile.detectSourceResidue(translation.text, {
        preservedSourceForms: [
          ...(policy.allowedLatinTokens ?? []),
          ...(source === undefined ? [] : isolatedSourceIdentifiers(source.sourceText)),
        ],
      });
      if (untranslated.length > 0) {
        failures.push({
          code: "untranslated_latin",
          blockId: translation.blockId,
          message: `unexpected source-language prose tokens: ${[
            ...new Set(untranslated.map((finding) => finding.form)),
          ].join(", ")}`,
          repairable: true,
        });
      }
      if (source === undefined) {
        continue;
      }
      for (const term of policy.requiredTerms ?? []) {
        if (source.sourceText.toLocaleLowerCase().includes(
          term.sourceForm.toLocaleLowerCase(),
        ) && !translation.text.includes(term.target)) {
          failures.push({
            code: "stable_term_mismatch",
            blockId: translation.blockId,
            message: `source form ${term.sourceForm} requires run-local target ${term.target}`,
            repairable: true,
          });
        }
      }
      const sourceParagraphs = paragraphCount(source.sourceText);
      const targetParagraphs = paragraphCount(translation.text);
      const minimumParagraphs = Math.max(1, Math.ceil(sourceParagraphs * 0.5));
      const maximumParagraphs = Math.max(2, sourceParagraphs * 2);
      if (targetParagraphs < minimumParagraphs || targetParagraphs > maximumParagraphs) {
        failures.push({
          code: "paragraph_count_incompatible",
          blockId: translation.blockId,
          message: `source paragraphs=${sourceParagraphs}, target paragraphs=${targetParagraphs}`,
          repairable: true,
        });
      }
      if (SOURCE_LAYOUT_TOKEN_PATTERN.test(translation.text)) {
        failures.push({
          code: "source_layout_token_leak",
          blockId: translation.blockId,
          message: "translation contains an extraction-only source layout token; preserve the scene break as an ordinary paragraph boundary",
          repairable: true,
        });
      }
      if (translation.text.includes("\uFFFD")) {
        failures.push({
          code: "invalid_unicode_output",
          blockId: translation.blockId,
          message: "translation contains the Unicode replacement character",
          repairable: true,
        });
      }
      if (hasInvalidUnicodeScalar(translation.text)) {
        failures.push({
          code: "invalid_unicode_output",
          blockId: translation.blockId,
          message: "translation contains invalid, noncharacter, or private-use Unicode scalars",
          repairable: true,
        });
      }
      if (/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F]/u.test(translation.text)) {
        failures.push({
          code: "invalid_unicode_output",
          blockId: translation.blockId,
          message: "translation contains prohibited Unicode control characters",
          repairable: true,
        });
      }
      const blockSourceLength = meaningfulLength(source.sourceText);
      const blockTargetLength = meaningfulLength(translation.text);
      const ratioBounds = [...(profile.translationLengthRatioBands ?? [])]
        .filter((band) => blockSourceLength >= band.minSourceCharacters)
        .sort((left, right) => right.minSourceCharacters - left.minSourceCharacters)[0];
      if (ratioBounds !== undefined) {
        const ratio = blockTargetLength / blockSourceLength;
        if (ratio < ratioBounds.min) {
          failures.push({
            code: "abnormal_block_shortening",
            blockId: translation.blockId,
            message: `target/source character ratio ${ratio.toFixed(3)} is below ${ratioBounds.min.toFixed(2)} for ${profile.id}`,
            repairable: true,
          });
        } else if (ratio > ratioBounds.max) {
          failures.push({
            code: "abnormal_block_expansion",
            blockId: translation.blockId,
            message: `target/source character ratio ${ratio.toFixed(3)} exceeds ${ratioBounds.max.toFixed(2)} for ${profile.id}`,
            repairable: true,
          });
        }
        const sourceLetterLength = letterGraphemeLength(source.sourceText);
        const targetLetterLength = letterGraphemeLength(translation.text);
        if (sourceLetterLength >= 8
          && targetLetterLength / sourceLetterLength < ratioBounds.min * 0.5) {
          failures.push({
            code: "insufficient_lexical_content",
            blockId: translation.blockId,
            message: `target readable-letter ratio ${(targetLetterLength / sourceLetterLength).toFixed(3)} is below ${(ratioBounds.min * 0.5).toFixed(2)} for ${profile.id}`,
            repairable: true,
          });
        }
        const isolatedIdentifiers = isolatedSourceIdentifiers(source.sourceText);
        const sourceIsIdentifierOnly = isolatedIdentifiers.includes(source.sourceText.trim());
        if (sourceLetterLength >= 8
          && !sourceIsIdentifierOnly
          && targetLetterLength > 0
          && hanGraphemeLength(translation.text) / targetLetterLength < 0.5) {
          failures.push({
            code: "target_script_mismatch",
            blockId: translation.blockId,
            message: "translation has insufficient Chinese-script prose for a zh-Hans target",
            repairable: true,
          });
        }
      }
      sourceLength += blockSourceLength;
      targetLength += blockTargetLength;
    }

    failures.push(...this.validateCrossBlockAlignment(blocks, candidate).failures);

    if (sourceLength > 0 && targetLength / sourceLength < 0.15) {
      failures.push({
        code: "abnormal_shortening",
        message: `target/source character ratio ${(targetLength / sourceLength).toFixed(3)} is below 0.15`,
        repairable: true,
      });
    }
    if (sourceLength > 0 && targetLength / sourceLength > 2) {
      failures.push({
        code: "abnormal_expansion",
        message: `target/source character ratio ${(targetLength / sourceLength).toFixed(3)} exceeds 2.0`,
        repairable: true,
      });
    }
    return { valid: failures.length === 0, failures };
  }
}
