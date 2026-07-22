import type {
  AnchorCandidateInput,
  BoundaryCandidate,
  ProfileAnchorCandidate,
  ResidueDetectionOptions,
  ResidueFinding,
  ScriptStats,
  SourceLanguageProfile,
  SourceScript,
  SourceToken,
  StructureHeading,
} from "./types.js";

const PROFILE_VERSION = "source-language-profile-2";
const DEFAULT_CANDIDATE_LIMIT = 24;

interface ProfileDefinition {
  id: string;
  displayName: string;
  locale: string;
  volumePatterns: readonly RegExp[];
  chapterPatterns: readonly RegExp[];
  stopWords: readonly string[];
  script: SourceScript;
  leadingContractions?: readonly string[];
  aliasCuePatterns?: readonly RegExp[];
  namingCuePatterns?: readonly RegExp[];
}

const DEFINITIONS: readonly ProfileDefinition[] = [
  {
    id: "en",
    displayName: "English",
    locale: "en",
    volumePatterns: [/^BOOK(?:\s+(?:\d+|[IVXLCDM]+|[A-Z]+))?$/iu],
    chapterPatterns: [
      /^CHAPTER(?:\s+(?:\d+|[IVXLCDM]+|[A-Z]+))?$/iu,
      /^[IVXLCDM]+$/u,
    ],
    stopWords: [
      "a", "after", "all", "an", "and", "another", "any", "as", "at",
      "before", "book", "but", "by", "chapter", "for", "from", "he", "her",
      "his", "i", "in", "is", "it", "its", "my", "of", "on", "or", "our",
      "she", "that", "the", "their", "they", "this", "to", "we", "with",
      "you", "your",
    ],
    script: "latin",
    aliasCuePatterns: [
      /\b(?:also known as|known as|called|alias|a\.?k\.?a\.?)\b/iu,
    ],
  },
  {
    id: "fr",
    displayName: "French",
    locale: "fr",
    volumePatterns: [/^LIVRE(?:\s+(?:\d+|[IVXLCDM]+|[\p{L}-]+))?$/iu],
    chapterPatterns: [
      /^CHAPITRE(?:\s+(?:\d+|[IVXLCDM]+|[\p{L}-]+))?$/iu,
      /^[IVXLCDM]+$/u,
    ],
    stopWords: [
      "au", "aux", "avec", "ce", "ces", "chapitre", "dans", "de", "des",
      "du", "elle", "en", "et", "il", "la", "le", "les", "livre", "mais",
      "ou", "par", "pour", "que", "qui", "sur", "un", "une",
    ],
    script: "latin",
    leadingContractions: ["c", "d", "j", "l", "m", "n", "qu", "s", "t"],
    aliasCuePatterns: [
      /\b(?:aussi connu(?:e)? sous le nom de|connu(?:e)? comme|appel[ée]|surnomm[ée])\b/iu,
    ],
  },
  {
    id: "de",
    displayName: "German",
    locale: "de",
    volumePatterns: [/^(?:BUCH|BAND)(?:\s+(?:\d+|[IVXLCDM]+|[\p{L}-]+))?$/iu],
    chapterPatterns: [/^KAPITEL(?:\s+(?:\d+|[IVXLCDM]+|[\p{L}-]+))?$/iu],
    stopWords: ["aber", "das", "der", "die", "ein", "eine", "er", "es", "im", "in", "mit", "sie", "und", "von", "zu"],
    script: "latin",
    aliasCuePatterns: [/\b(?:auch bekannt als|bekannt als|genannt)\b/iu],
  },
  {
    id: "es",
    displayName: "Spanish",
    locale: "es",
    volumePatterns: [/^LIBRO(?:\s+(?:\d+|[IVXLCDM]+|[\p{L}-]+))?$/iu],
    chapterPatterns: [/^CAP[IÍ]TULO(?:\s+(?:\d+|[IVXLCDM]+|[\p{L}-]+))?$/iu],
    stopWords: ["a", "con", "de", "del", "el", "ella", "en", "la", "las", "los", "pero", "por", "que", "se", "un", "una", "y"],
    script: "latin",
    aliasCuePatterns: [
      /\b(?:tambi[ée]n conocid[oa] como|conocid[oa] como|llamad[oa])\b/iu,
    ],
  },
  {
    id: "ru",
    displayName: "Russian",
    locale: "ru",
    volumePatterns: [/^(?:КНИГА|ТОМ)(?:\s+[\p{L}\p{N}-]+)?$/iu],
    chapterPatterns: [/^ГЛАВА(?:\s+[\p{L}\p{N}-]+)?$/iu],
    stopWords: ["а", "в", "и", "из", "к", "на", "но", "он", "она", "с", "что"],
    script: "cyrillic",
    aliasCuePatterns: [/(?:также известн|известн.+ как|по прозвищу)/iu],
  },
  {
    id: "ja",
    displayName: "Japanese",
    locale: "ja",
    volumePatterns: [
      /^\u7b2c[^\r\n]{1,20}\u5dfb$/u,
      /^[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{N}\u30fc]{2,24}\u306e\u5dfb$/u,
    ],
    chapterPatterns: [
      /^\u7b2c[^\r\n]{1,20}\u7ae0$/u,
      /^\u7b2c[^\r\n]{1,20}\u8a71$/u,
      /^(?:\u5e8f\u7ae0|\u7d42\u7ae0|\u30d7\u30ed\u30ed\u30fc\u30b0|\u30a8\u30d4\u30ed\u30fc\u30b0)$/u,
    ],
    stopWords: [
      "\u3053\u308c", "\u305d\u308c", "\u3042\u308c", "\u3053\u3068", "\u3082\u306e", "\u305f\u3081", "\u3055\u3089\u306b",
      "\u3059\u308b", "\u3057\u305f", "\u3057\u3066", "\u3042\u308b", "\u3042\u3063\u305f", "\u3067\u3059", "\u307e\u3059",
    ],
    script: "kana",
    aliasCuePatterns: [/(?:\u5225\u540d|\u3068\u3057\u3066\u77e5\u3089)/u],
    namingCuePatterns: [/(?:\u6c0f|\u69d8|\u6bbf|\u3055\u3093)\b/u],
  },
  {
    id: "ko",
    displayName: "Korean",
    locale: "ko",
    volumePatterns: [
      /^\uc81c\s*(?:\d+|[\p{Script=Hangul}\p{Script=Han}\p{N}]+)\s*\uad8c$/u,
    ],
    chapterPatterns: [
      /^\uc81c\s*(?:\d+|[\p{Script=Hangul}\p{Script=Han}\p{N}]+)\s*\uc7a5$/u,
      /^\d+\s*\uc7a5$/u,
      /^(?:\uc11c\uc7a5|\uc885\uc7a5|\ud504\ub864\ub85c\uadf8|\uc5d0\ud544\ub85c\uadf8)$/u,
      /^(?:\[|\u3010)\s*[\p{L}\p{N}\s-]{1,32}\s*(?:\]|\u3011)$/u,
    ],
    stopWords: [
      "\uadf8\ub7ec\ub098", "\uadf8\ub9ac\uace0", "\uadf8\ub7f0", "\uc774\ub7f0", "\uac83\uc740", "\uac83\uc774", "\uc5c6\uc5c8\ub2e4",
      "\uc788\uc5c8\ub2e4", "\ud588\ub2e4", "\ub418\uc5c8\ub2e4", "\ud558\uc5c8\ub2e4", "\ud558\ub294", "\uc774\ub2e4",
    ],
    script: "hangul",
    aliasCuePatterns: [/(?:\ubcc4\uba85|\ubd88\ub9ac|\ub77c\uace0\s*\ud55c\ub2e4)/u],
    namingCuePatterns: [/(?:\uc528|\ub2d8|\uc7a5\uad70|\ub300\uac10)/u],
  },
  {
    id: "und",
    displayName: "Undetermined",
    locale: "und",
    volumePatterns: [],
    chapterPatterns: [],
    stopWords: [],
    script: "unknown",
    aliasCuePatterns: [/\b(?:alias|a\.?k\.?a\.?)\b/iu],
  },
];

function normalizeApostrophes(value: string): string {
  return value.replace(/[’‘`]/gu, "'");
}

function normalizeForm(value: string, definition: ProfileDefinition): string {
  let normalized = normalizeApostrophes(value.normalize("NFKC"))
    .trim()
    .toLocaleLowerCase(definition.locale);
  if (definition.id === "en") {
    normalized = normalized.replace(/'s$/u, "");
  }
  if (definition.leadingContractions !== undefined) {
    const contraction = new RegExp(
      `^(?:${definition.leadingContractions.join("|")})['’]`,
      "iu",
    );
    normalized = normalized.replace(contraction, "");
  }
  return normalized;
}

function segmentText(text: string, definition: ProfileDefinition): SourceToken[] {
  const segmenter = new Intl.Segmenter(definition.locale, { granularity: "word" });
  return [...segmenter.segment(text)].map((part) => ({
    value: part.segment,
    normalized: normalizeForm(part.segment, definition),
    start: part.index,
    end: part.index + part.segment.length,
    isWordLike: part.isWordLike ?? false,
  }));
}

function scriptsFor(definition: ProfileDefinition): readonly SourceScript[] {
  switch (definition.script) {
    case "kana":
      return ["han", "kana"];
    case "hangul":
      return ["han", "hangul"];
    case "latin":
      return ["latin"];
    case "cyrillic":
      return ["cyrillic"];
    case "han":
      return ["han"];
    default:
      return ["unknown"];
  }
}

function scriptStats(text: string): ScriptStats {
  const stats: ScriptStats = {
    scalars: 0,
    latin: 0,
    han: 0,
    kana: 0,
    hangul: 0,
    other: 0,
  };
  for (const scalar of text) {
    stats.scalars += 1;
    if (/\p{Script=Latin}/u.test(scalar)) {
      stats.latin += 1;
    } else if (/\p{Script=Han}/u.test(scalar)) {
      stats.han += 1;
    } else if (/[\p{Script=Hiragana}\p{Script=Katakana}\u30fc]/u.test(scalar)) {
      stats.kana += 1;
    } else if (/\p{Script=Hangul}/u.test(scalar)) {
      stats.hangul += 1;
    } else {
      stats.other += 1;
    }
  }
  return stats;
}

function structureHeading(
  line: string,
  definition: ProfileDefinition,
): StructureHeading | null {
  const title = line.trim();
  if (definition.volumePatterns.some((pattern) => pattern.test(title))) {
    return { kind: "volume_heading", title, boundaryWeight: 100 };
  }
  if (definition.chapterPatterns.some((pattern) => pattern.test(title))) {
    return { kind: "chapter_heading", title, boundaryWeight: 80 };
  }
  return null;
}

const CLOSING_PUNCTUATION = new Set([
  "\"", "'", "\u201d", "\u2019", "\u300d", "\u300f", "\uff09", "\u3011", "\u3015",
]);

interface Utf16BoundaryCandidate {
  utf16Offset: number;
  weight: number;
  kind: BoundaryCandidate["kind"];
}

function appendUtf16BoundariesAsScalars(
  scalars: readonly string[],
  boundaries: readonly Utf16BoundaryCandidate[],
  candidates: BoundaryCandidate[],
): void {
  const ordered = [...boundaries].sort((left, right) => (
    left.utf16Offset - right.utf16Offset
    || right.weight - left.weight
    || left.kind.localeCompare(right.kind)
  ));
  let boundaryIndex = 0;
  let utf16Offset = 0;
  while ((ordered[boundaryIndex]?.utf16Offset ?? Number.POSITIVE_INFINITY) <= 0) {
    const boundary = ordered[boundaryIndex] as Utf16BoundaryCandidate;
    candidates.push({
      scalarOffset: 0,
      weight: boundary.weight,
      kind: boundary.kind,
    });
    boundaryIndex += 1;
  }
  for (let scalarIndex = 0; scalarIndex < scalars.length; scalarIndex += 1) {
    const scalar = scalars[scalarIndex] as string;
    const nextUtf16Offset = utf16Offset + scalar.length;
    while ((ordered[boundaryIndex]?.utf16Offset ?? Number.POSITIVE_INFINITY)
      <= nextUtf16Offset) {
      const boundary = ordered[boundaryIndex] as Utf16BoundaryCandidate;
      candidates.push({
        scalarOffset: boundary.utf16Offset <= utf16Offset
          ? scalarIndex
          : scalarIndex + 1,
        weight: boundary.weight,
        kind: boundary.kind,
      });
      boundaryIndex += 1;
    }
    utf16Offset = nextUtf16Offset;
  }
}

function cjkBoundaryCandidates(
  text: string,
  definition: ProfileDefinition,
): BoundaryCandidate[] {
  const candidates: BoundaryCandidate[] = [];
  const utf16Boundaries: Utf16BoundaryCandidate[] = [];
  for (const match of text.matchAll(/(?:\r\n|\r|\n)[ \t]*(?:(?:\r\n|\r|\n)[ \t]*)+/gu)) {
    if (match.index !== undefined) {
      utf16Boundaries.push({
        utf16Offset: match.index + match[0].length,
        weight: 70,
        kind: "paragraph",
      });
    }
  }

  const scalars = [...text];
  for (let index = 0; index < scalars.length; index += 1) {
    const scalar = scalars[index] as string;
    const cjkTerminal = scalar === "\u3002" || scalar === "\uff01" || scalar === "\uff1f";
    if (!cjkTerminal && scalar !== "." && scalar !== "!" && scalar !== "?") {
      continue;
    }
    let end = index + 1;
    while (end < scalars.length && CLOSING_PUNCTUATION.has(scalars[end] as string)) {
      end += 1;
    }
    const next = scalars[end];
    if (!cjkTerminal && next !== undefined && !/\s/u.test(next)) {
      continue;
    }
    candidates.push({ scalarOffset: end, weight: 50, kind: "sentence" });
    index = end - 1;
  }

  for (const match of text.matchAll(/^.*$/gmu)) {
    if (match.index === undefined || match[0].length === 0) {
      continue;
    }
    const heading = structureHeading(match[0], definition);
    if (heading !== null) {
      utf16Boundaries.push({
        utf16Offset: match.index,
        weight: heading.boundaryWeight,
        kind: "heading",
      });
    }
  }
  appendUtf16BoundariesAsScalars(scalars, utf16Boundaries, candidates);

  const strongestByOffset = new Map<number, BoundaryCandidate>();
  for (const candidate of candidates) {
    const previous = strongestByOffset.get(candidate.scalarOffset);
    if (previous === undefined || candidate.weight > previous.weight) {
      strongestByOffset.set(candidate.scalarOffset, candidate);
    }
  }
  return [...strongestByOffset.values()].sort((left, right) => (
    left.scalarOffset - right.scalarOffset
    || right.weight - left.weight
    || left.kind.localeCompare(right.kind)
  ));
}

function compactContext(text: string, offset: number): string {
  const before = text.slice(0, offset);
  const left = Math.max(
    before.lastIndexOf("."),
    before.lastIndexOf("?"),
    before.lastIndexOf("!"),
    before.lastIndexOf("。"),
    before.lastIndexOf("？"),
    before.lastIndexOf("！"),
    before.lastIndexOf("\n"),
  );
  const after = text.slice(offset);
  const candidates = [".", "?", "!", "。", "？", "！", "\n"]
    .map((mark) => after.indexOf(mark))
    .filter((index) => index >= 0);
  const right = candidates.length === 0
    ? text.length
    : offset + Math.min(...candidates) + 1;
  return text.slice(left + 1, right).replace(/\s+/gu, " ").trim().slice(0, 360);
}

function cjkCandidateToken(token: SourceToken, definition: ProfileDefinition): boolean {
  if (!/^[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}\u30fc]+$/u.test(token.value)) {
    return false;
  }
  if (definition.script === "kana") {
    return /[\p{Script=Han}\p{Script=Katakana}]/u.test(token.value);
  }
  return /\p{Script=Hangul}/u.test(token.value);
}

function isCandidateToken(token: SourceToken, definition: ProfileDefinition): boolean {
  if (!token.isWordLike || token.normalized.length < 2) {
    return false;
  }
  if (definition.stopWords.includes(token.normalized)) {
    return false;
  }
  if (definition.script === "latin") {
    return /^\p{Lu}[\p{L}\p{M}\p{N}'’.-]{1,}$/u.test(token.value);
  }
  if (definition.script === "cyrillic") {
    return /^\p{Lu}[\p{Script=Cyrillic}\p{M}-]{1,}$/u.test(token.value);
  }
  if (definition.script === "kana" || definition.script === "hangul") {
    return cjkCandidateToken(token, definition);
  }
  return false;
}

function collectCandidates(
  input: AnchorCandidateInput,
  definition: ProfileDefinition,
): ProfileAnchorCandidate[] {
  const requestedLimit = input.limit ?? DEFAULT_CANDIDATE_LIMIT;
  if (!Number.isSafeInteger(requestedLimit) || requestedLimit < 0) {
    throw new TypeError("candidate limit must be a non-negative safe integer");
  }
  const limit = Math.min(requestedLimit, DEFAULT_CANDIDATE_LIMIT);
  const established = new Set((input.establishedSourceForms ?? [])
    .map((form) => normalizeForm(form, definition)));
  const current = new Map<string, { sourceForm: string; count: number }>();
  for (const text of input.targetTexts) {
    for (const token of segmentText(text, definition)) {
      if (!isCandidateToken(token, definition) || established.has(token.normalized)) {
        continue;
      }
      const record = current.get(token.normalized) ?? {
        sourceForm: token.value.replace(/[’']s$/u, ""),
        count: 0,
      };
      record.count += 1;
      current.set(token.normalized, record);
    }
  }

  const corpus = new Map<string, {
    count: number;
    candidateCaseCount: number;
    contexts: Set<string>;
    documentIndexes: Set<number>;
  }>();
  for (const [documentIndex, text] of input.corpusTexts.entries()) {
    for (const token of segmentText(text, definition)) {
      if (!current.has(token.normalized)) {
        continue;
      }
      const record = corpus.get(token.normalized) ?? {
        count: 0,
        candidateCaseCount: 0,
        contexts: new Set<string>(),
        documentIndexes: new Set<number>(),
      };
      record.count += 1;
      record.documentIndexes.add(documentIndex);
      if (isCandidateToken(token, definition)) {
        record.candidateCaseCount += 1;
      }
      if (record.contexts.size < 3) {
        const context = compactContext(text, token.start);
        if (context.length > 0) {
          record.contexts.add(context);
        }
      }
      corpus.set(token.normalized, record);
    }
  }

  return [...current.entries()].flatMap(([normalizedSource, wave]) => {
    const evidence = corpus.get(normalizedSource) ?? {
      count: wave.count,
      candidateCaseCount: wave.count,
      contexts: new Set<string>(),
      documentIndexes: new Set<number>(),
    };
    const contexts = [...evidence.contexts];
    const hasAliasCue = contexts.some((context) =>
      definition.aliasCuePatterns?.some((pattern) => pattern.test(context)) ?? false);
    const hasNamingCue = contexts.some((context) =>
      definition.namingCuePatterns?.some((pattern) => pattern.test(context)) ?? false);
    const caseConsistency = evidence.count === 0
      ? 1
      : evidence.candidateCaseCount / evidence.count;
    const caseAmbiguous = evidence.candidateCaseCount < evidence.count
      && caseConsistency <= 0.5;
    if (caseAmbiguous && !hasAliasCue && !hasNamingCue) {
      return [];
    }
    const isCjk = definition.script === "kana" || definition.script === "hangul";
    if (isCjk && evidence.count < 2 && wave.count < 2 && !hasAliasCue && !hasNamingCue) {
      return [];
    }
    const positionalSpread = Math.min(evidence.documentIndexes.size, 3) * 5
      + Math.min(contexts.length, 3) * 3;
    const score = 20
      + Math.log1p(evidence.count) * 8
      + Math.min(wave.count, 4) * 3
      + positionalSpread
      + caseConsistency * 30
      - (1 - caseConsistency) * 35
      + (hasAliasCue ? 80 : 0)
      + (hasNamingCue ? 20 : 0);
    return [{
      sourceForm: wave.sourceForm,
      normalizedSource,
      contexts,
      corpusFrequency: evidence.count,
      currentWaveOccurrences: wave.count,
      score,
    }];
  }).sort((left, right) => right.score - left.score
    || left.sourceForm.localeCompare(right.sourceForm))
    .slice(0, limit);
}

function overlapsUrl(text: string, start: number, end: number): boolean {
  for (const match of text.matchAll(/https?:\/\/\S+|\b\S+\.\p{L}{2,}(?:\/\S*)?/giu)) {
    const matchStart = match.index;
    const matchEnd = matchStart + match[0].length;
    if (start < matchEnd && end > matchStart) {
      return true;
    }
  }
  return false;
}

function latinResidue(
  text: string,
  definition: ProfileDefinition,
  options: ResidueDetectionOptions,
): ResidueFinding[] {
  const preserved = new Set((options.preservedSourceForms ?? [])
    .flatMap((form) => segmentText(form, definition))
    .filter((token) => token.isWordLike)
    .map((token) => token.normalized));
  return segmentText(text, definition).flatMap((token): ResidueFinding[] => {
    if (!token.isWordLike
      || !/^\p{Script=Latin}[\p{Script=Latin}\p{M}'’-]+$/u.test(token.value)
      || token.normalized.length < 2
      || preserved.has(token.normalized)
      || /^[\p{Lu}\d]{2,6}$/u.test(token.value)
      || /\d/u.test(token.value)
      || overlapsUrl(text, token.start, token.end)) {
      return [];
    }
    const neighborhood = text.slice(Math.max(0, token.start - 3), token.end + 3);
    if (/[=+*/^]\s*\p{L}|\p{L}\s*[=+*/^]/u.test(neighborhood)) {
      return [];
    }
    return [{
      code: "source_prose_residue",
      form: token.value,
      start: token.start,
      end: token.end,
      script: "latin",
    }];
  });
}

function scriptedResidue(
  text: string,
  definition: ProfileDefinition,
  options: ResidueDetectionOptions,
): ResidueFinding[] {
  const preserved = new Set((options.preservedSourceForms ?? [])
    .map((form) => normalizeForm(form, definition)));
  const pattern = definition.script === "cyrillic"
    ? /[\p{Script=Cyrillic}\p{M}]{2,}/gu
    : definition.script === "hangul"
      ? /[\p{Script=Hangul}]{2,}/gu
      : /[\p{Script=Hiragana}\p{Script=Katakana}\u30fc]{2,}/gu;
  return [...text.matchAll(pattern)].flatMap((match): ResidueFinding[] => {
    const form = match[0];
    if (preserved.has(normalizeForm(form, definition))) {
      return [];
    }
    return [{
      code: "source_prose_residue",
      form,
      start: match.index,
      end: match.index + form.length,
      script: definition.script === "cyrillic"
        ? "cyrillic"
        : definition.script === "hangul"
          ? "hangul"
          : "kana",
    }];
  });
}

function buildProfile(definition: ProfileDefinition): SourceLanguageProfile {
  const profile: SourceLanguageProfile = {
    id: definition.id,
    version: PROFILE_VERSION,
    displayName: definition.displayName,
    locale: definition.locale,
    scripts: scriptsFor(definition),
    detectStructureHeading(line: string): StructureHeading | null {
      return structureHeading(line, definition);
    },
    collectBoundaryCandidates(text: string): BoundaryCandidate[] {
      return cjkBoundaryCandidates(text, definition);
    },
    collectScriptStats(text: string): ScriptStats {
      return scriptStats(text);
    },
    segment(text: string): SourceToken[] {
      return segmentText(text, definition);
    },
    normalizeSourceForm(text: string): string {
      return normalizeForm(text, definition);
    },
    collectAnchorCandidates(input: AnchorCandidateInput): ProfileAnchorCandidate[] {
      return collectCandidates(input, definition);
    },
    detectSourceResidue(
      translation: string,
      options: ResidueDetectionOptions = {},
    ): ResidueFinding[] {
      if (definition.script === "latin") {
        return latinResidue(translation, definition, options);
      }
      if (definition.script === "cyrillic"
        || definition.script === "kana"
        || definition.script === "hangul") {
        return scriptedResidue(translation, definition, options);
      }
      return [];
    },
  };
  return Object.freeze(profile);
}

const PROFILES = new Map(DEFINITIONS.map((definition) => [
  definition.id,
  buildProfile(definition),
]));

export function supportedSourceLanguageIds(): string[] {
  return [...PROFILES.keys()].sort();
}

export function getSourceLanguageProfile(language: string | undefined): SourceLanguageProfile {
  const primary = language?.trim().toLocaleLowerCase().split("-", 1)[0] ?? "und";
  return PROFILES.get(primary) ?? PROFILES.get("und") as SourceLanguageProfile;
}
