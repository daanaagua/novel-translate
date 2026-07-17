import type { V4Block } from "./domain/types.js";

export interface PilotTranslation {
  blockId: string;
  globalIndex: number;
  chapterId: string | null;
  chapterTitle: string | null;
  sourceText: string;
  text: string;
}

export function renderTranslation(translations: readonly PilotTranslation[]): string {
  const lines: string[] = [];
  let chapter: string | null | undefined;
  for (const item of translations) {
    if (item.chapterId !== chapter) {
      chapter = item.chapterId;
      lines.push(
        lines.length === 0 ? "" : "\n",
        `# ${item.chapterTitle ?? item.chapterId ?? "Untitled"}`,
        "",
      );
    }
    lines.push(item.text.trim(), "");
  }
  return `${lines.join("\n").trim()}\n`;
}

export function renderBilingual(translations: readonly PilotTranslation[]): string {
  const lines: string[] = [];
  for (const item of translations) {
    lines.push(
      `## ${item.blockId} · global ${item.globalIndex}`,
      "",
      "[SOURCE]",
      item.sourceText.trim(),
      "",
      "[TRANSLATION]",
      item.text.trim(),
      "",
    );
  }
  return `${lines.join("\n").trim()}\n`;
}

export function joinTranslations(
  blocks: readonly V4Block[],
  translations: ReadonlyMap<string, string>,
): PilotTranslation[] {
  return blocks
    .filter((block) => translations.has(block.id))
    .map((block) => ({
      blockId: block.id,
      globalIndex: block.globalIndex,
      chapterId: block.chapterId,
      chapterTitle: block.chapterTitle,
      sourceText: block.sourceText,
      text: translations.get(block.id) as string,
    }));
}
