export interface ChineseQuoteNormalizationState {
  openDoubleQuoteDepth: number;
}

export interface ChineseQuoteTextNormalization {
  text: string;
  state: ChineseQuoteNormalizationState;
}

export interface ChineseQuoteTextsNormalization {
  texts: string[];
  state: ChineseQuoteNormalizationState;
}

const OPENING_DOUBLE_QUOTE_GLYPHS = new Set([
  "“",
  "‛",
  "‟",
  "〝",
  "„",
  "「",
  "『",
]);

const CLOSING_DOUBLE_QUOTE_GLYPHS = new Set([
  "”",
  "〞",
  "」",
  "』",
]);

const OPENING_CONTEXT_GLYPHS = new Set([
  "(", "（", "[", "［", "{", "｛", "〈", "《", "「", "『", "【", "〔",
  ":", "：", ";", "；", ",", "，", "、", "—", "–", "-",
]);

function normalizedState(state: ChineseQuoteNormalizationState | undefined): ChineseQuoteNormalizationState {
  return {
    openDoubleQuoteDepth: Math.max(0, Math.trunc(state?.openDoubleQuoteDepth ?? 0)),
  };
}

function isOpeningAsciiQuote(
  previousSignificant: string | undefined,
  atLineStart: boolean,
  openDoubleQuoteDepth: number,
): boolean {
  if (atLineStart || previousSignificant === undefined) {
    return true;
  }
  if (openDoubleQuoteDepth > 0) {
    return previousSignificant === ":" || previousSignificant === "：";
  }
  return OPENING_CONTEXT_GLYPHS.has(previousSignificant);
}

/**
 * Rewrites every validator-forbidden double-quote glyph one-for-one into the
 * Chinese curly quotation system. ASCII quotes are resolved with a small
 * state machine rather than a pairing regex, so unmatched closing marks stay
 * closing marks and remain visible to quote_boundary_mismatch validation.
 */
export function normalizeChineseQuoteText(
  text: string,
  initialState?: ChineseQuoteNormalizationState,
): ChineseQuoteTextNormalization {
  let { openDoubleQuoteDepth } = normalizedState(initialState);
  let previousSignificant: string | undefined;
  let atLineStart = true;
  const result: string[] = [];

  for (const glyph of Array.from(text)) {
    let normalizedGlyph = glyph;
    if (glyph === "\"") {
      const opening = isOpeningAsciiQuote(
        previousSignificant,
        atLineStart,
        openDoubleQuoteDepth,
      );
      normalizedGlyph = opening ? "“" : "”";
      openDoubleQuoteDepth += opening ? 1 : openDoubleQuoteDepth > 0 ? -1 : 0;
    } else if (OPENING_DOUBLE_QUOTE_GLYPHS.has(glyph)) {
      normalizedGlyph = "“";
      openDoubleQuoteDepth += 1;
    } else if (CLOSING_DOUBLE_QUOTE_GLYPHS.has(glyph)) {
      normalizedGlyph = "”";
      openDoubleQuoteDepth = Math.max(0, openDoubleQuoteDepth - 1);
    }
    result.push(normalizedGlyph);

    if (glyph === "\r" || glyph === "\n") {
      atLineStart = true;
      continue;
    }
    if (!/\s/u.test(glyph)) {
      atLineStart = false;
      previousSignificant = normalizedGlyph;
    }
  }

  return {
    text: result.join(""),
    state: { openDoubleQuoteDepth },
  };
}

/** Applies quote normalization in order without changing text block boundaries. */
export function normalizeChineseQuoteTexts(
  texts: readonly string[],
  initialState?: ChineseQuoteNormalizationState,
): ChineseQuoteTextsNormalization {
  let state = normalizedState(initialState);
  const normalizedTexts: string[] = [];
  for (const text of texts) {
    const normalized = normalizeChineseQuoteText(text, state);
    normalizedTexts.push(normalized.text);
    state = normalized.state;
  }
  return { texts: normalizedTexts, state };
}
