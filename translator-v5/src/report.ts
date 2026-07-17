import type { V4Block } from "./domain/types.js";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import type { BookStore } from "./storage/book-store.js";

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

export interface BookArtifactPaths {
  translation: string;
  bilingual: string;
  audit: string;
  metrics: string;
}

export function writeBookArtifacts(
  store: BookStore,
  outputDirectory: string,
  options: { allowIncomplete?: boolean } = {},
): BookArtifactPaths {
  const status = store.statusSummary();
  if (!options.allowIncomplete && status.translatedBlocks !== status.totalBlocks) {
    throw new Error(
      `strict book export requires ${status.totalBlocks} translated blocks; found ${status.translatedBlocks}`,
    );
  }
  const translations: PilotTranslation[] = store.activeTranslations().map((item) => ({
    blockId: item.blockId,
    globalIndex: item.globalIndex,
    chapterId: item.chapterId,
    chapterTitle: item.chapterTitle,
    sourceText: item.sourceText,
    text: item.text,
  }));
  mkdirSync(outputDirectory, { recursive: true });
  const paths = {
    translation: join(outputDirectory, "v5_book_translation.txt"),
    bilingual: join(outputDirectory, "v5_book_bilingual.txt"),
    audit: join(outputDirectory, "v5_book_audit.json"),
    metrics: join(outputDirectory, "v5_book_metrics.json"),
  };
  writeFileSync(paths.translation, renderTranslation(translations), "utf8");
  writeFileSync(paths.bilingual, renderBilingual(translations), "utf8");
  writeFileSync(paths.audit, `${JSON.stringify({
    schemaVersion: "v5-book-audit-1",
    status,
    windows: store.allWindows(),
  }, null, 2)}\n`, "utf8");
  writeFileSync(paths.metrics, `${JSON.stringify(status, null, 2)}\n`, "utf8");
  return paths;
}
