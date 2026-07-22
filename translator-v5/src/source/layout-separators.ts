/**
 * Some text extractions encode a scene break as the literal token `[[]]`.
 * It is source layout, not prose: keep it in the certified source ledger, but
 * force the following scene into a fresh translation block and never expose
 * the extraction token in Chinese output.
 */
const EMBEDDED_SCENE_SEPARATOR = /\[\[\]\]/gu;

export function embeddedSceneSeparatorEndOffsets(text: string): number[] {
  return [...text.matchAll(EMBEDDED_SCENE_SEPARATOR)].flatMap((match) => (
    match.index === undefined ? [] : [match.index + match[0].length]
  ));
}

export function normalizeSourceSceneSeparators(text: string): string {
  return text.replace(/[ \t]*\[\[\]\][ \t]*/gu, "\n\n");
}

export function normalizeTranslatedSceneSeparators(text: string): string {
  return normalizeSourceSceneSeparators(text)
    .replace(/[ \t]*\[\][ \t]*/gu, "\n\n")
    .replace(/\n[ \t]*\n(?:[ \t]*\n)+/gu, "\n\n");
}
