import { createHash } from "node:crypto";

import type {
  SourceInput,
  StructureAnnotation,
  StructureKind,
} from "./types.js";
import { coordinatesFor, sourceTextFor } from "./types.js";

const VOLUME_PATTERN = /^(?:BOOK[ \t]+(?:\d+|[IVXLCDM]+|ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN)|第[^\r\n]{0,40}卷)[ \t]*(?=\r?$)/gimu;
const CHAPTER_PATTERN = /^(?:CHAPTER(?:[ \t]+(?:\d+|[IVXLCDM]+|ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN))?|[IVXLCDM]+|第[^\r\n]{0,40}章)[ \t]*(?=\r?$)/gimu;

function annotationId(
  sourceVersion: string,
  kind: StructureKind,
  start: number,
  end: number,
): string {
  return `structure-${createHash("sha256")
    .update(`${sourceVersion}\0${kind}\0${start}\0${end}`)
    .digest("hex")
    .slice(0, 20)}`;
}

function collect(
  source: SourceInput,
  sourceVersion: string,
  kind: StructureKind,
  pattern: RegExp,
  boundaryWeight: number,
): StructureAnnotation[] {
  const text = sourceTextFor(source);
  const coordinates = coordinatesFor(source);
  const annotations: StructureAnnotation[] = [];
  for (const match of text.matchAll(pattern)) {
    if (match.index === undefined || match[0].length === 0) {
      continue;
    }
    const start = coordinates.toScalarIndex(match.index);
    const end = coordinates.toScalarIndex(match.index + match[0].length);
    annotations.push({
      id: annotationId(sourceVersion, kind, start, end),
      kind,
      start,
      end,
      title: match[0].trim(),
      boundaryWeight,
    });
  }
  return annotations;
}

export function annotateStructure(
  source: SourceInput,
  sourceVersion: string,
): StructureAnnotation[] {
  if (sourceVersion.trim().length === 0) {
    throw new TypeError("sourceVersion must not be empty");
  }
  return [
    ...collect(source, sourceVersion, "volume_heading", VOLUME_PATTERN, 100),
    ...collect(source, sourceVersion, "chapter_heading", CHAPTER_PATTERN, 80),
  ].sort((left, right) => (
    left.start - right.start
    || left.end - right.end
    || left.kind.localeCompare(right.kind)
  ));
}
