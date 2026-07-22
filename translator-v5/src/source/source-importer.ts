import { createHash, randomUUID } from "node:crypto";
import {
  mkdir,
  readFile,
  realpath,
  rename,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import { basename, dirname, extname, isAbsolute, join, posix, relative, resolve, sep } from "node:path";
import { TextDecoder } from "node:util";

import { XMLParser } from "fast-xml-parser";
import * as yauzl from "yauzl";
import type { Entry, ZipFile } from "yauzl";

import { detectLanguage, type DetectedLanguage } from "./language-detector.js";
import { SourceLedger } from "./source-ledger.js";
import { scalarLength, type CanonicalSegment, type ExcludedRawRange } from "./types.js";

const MAX_ARCHIVE_ENTRY_BYTES = 64 * 1024 * 1024;
const MAX_ARCHIVE_EXPANDED_BYTES = 512 * 1024 * 1024;
const XML_CONFIGURATION = {
  attributeNamePrefix: "@_",
  ignoreAttributes: false,
  parseTagValue: false,
  processEntities: false,
  removeNSPrefix: false,
  trimValues: false,
} as const;
const XML = new XMLParser(XML_CONFIGURATION);
const ORDERED_XML = new XMLParser({ ...XML_CONFIGURATION, preserveOrder: true });

type OrderedXmlNode = Record<string, unknown>;
type OrderedXmlNodes = readonly OrderedXmlNode[];

export type SourceImportErrorCode =
  | "SOURCE_INPUT_INVALID"
  | "SOURCE_FORMAT_UNSUPPORTED"
  | "ENCODING_AMBIGUOUS"
  | "SOURCE_CHANGED_DURING_IMPORT"
  | "PROJECT_EXISTS"
  | "ARCHIVE_INVALID"
  | "ARCHIVE_ENTRY_INVALID"
  | "ARCHIVE_ENTRY_TOO_LARGE"
  | "ARCHIVE_TOO_LARGE"
  | "DOCX_DOCUMENT_MISSING"
  | "DOCX_DOCUMENT_INVALID"
  | "EPUB_CONTAINER_MISSING"
  | "EPUB_OPF_MISSING"
  | "EPUB_SPINE_INVALID";

export class SourceImportError extends Error {
  readonly code: SourceImportErrorCode;

  constructor(code: SourceImportErrorCode, message: string) {
    super(`${code}: ${message}`);
    this.name = "SourceImportError";
    this.code = code;
  }
}

export interface SourceImportRequest {
  sourcePath: string;
  projectDirectory: string;
  sourceLanguage: string;
  explicitEncoding?: string;
}

export interface SourceImportResult {
  manifestPath: string;
  rawSha256: string;
  canonicalChars: number;
  detectedLanguage?: DetectedLanguage;
}

export interface SourceImporterDependencies {
  /** The injectable reader exists to make source-change detection deterministic in tests. */
  readSource?: (sourcePath: string) => Promise<Buffer>;
}

interface ExtractedPart {
  text: string;
  originKind: string;
  originRef: string;
  transformation: string;
}

interface ExtractedSource {
  sourceText: string;
  canonicalSegments: CanonicalSegment[];
  encoding: string;
  extractor: string;
  excludedRawRanges: ExcludedRawRange[];
}

interface Archive {
  zip: ZipFile;
  entries: ReadonlyMap<string, Entry>;
  extractedBytes: number;
}

function sha256(value: Buffer | string): string {
  return createHash("sha256").update(value).digest("hex");
}

function sourceError(code: SourceImportErrorCode, message: string): never {
  throw new SourceImportError(code, message);
}

function normalizeNewlines(value: string): string {
  return value.replace(/\r\n?|\u0085|\u2028|\u2029/gu, "\n");
}

function scalarSegments(parts: readonly ExtractedPart[]): Pick<ExtractedSource, "sourceText" | "canonicalSegments"> {
  let sourceText = "";
  const canonicalSegments: CanonicalSegment[] = [];
  for (let index = 0; index < parts.length; index += 1) {
    const part = parts[index]!;
    const start = scalarLength(sourceText);
    sourceText += normalizeNewlines(part.text);
    if (index < parts.length - 1) {
      sourceText += "\n\n";
    }
    canonicalSegments.push({
      canonicalStart: start,
      canonicalEnd: scalarLength(sourceText),
      originKind: part.originKind,
      originRef: part.originRef,
      transformation: part.transformation,
    });
  }
  return { sourceText, canonicalSegments };
}

function sourceFormat(path: string): ".txt" | ".md" | ".markdown" | ".docx" | ".epub" {
  const extension = extname(path).toLocaleLowerCase("en");
  if (extension === ".txt" || extension === ".md" || extension === ".markdown"
    || extension === ".docx" || extension === ".epub") {
    return extension;
  }
  return sourceError("SOURCE_FORMAT_UNSUPPORTED", `unsupported source extension: ${extension || "(none)"}`);
}

function normalizeEncoding(value: string): "utf-8" | "utf-16le" | "utf-16be" | "utf-32le" | "utf-32be" {
  switch (value.trim().toLocaleLowerCase("en").replaceAll("_", "-")) {
    case "utf8":
    case "utf-8":
      return "utf-8";
    case "utf16le":
    case "utf-16le":
      return "utf-16le";
    case "utf16be":
    case "utf-16be":
      return "utf-16be";
    case "utf32le":
    case "utf-32le":
      return "utf-32le";
    case "utf32be":
    case "utf-32be":
      return "utf-32be";
    default:
      return sourceError("ENCODING_AMBIGUOUS", `unsupported explicit encoding: ${value}`);
  }
}

function utf32Decode(payload: Buffer, littleEndian: boolean): string {
  if (payload.length % 4 !== 0) {
    return sourceError("ENCODING_AMBIGUOUS", "UTF-32 payload is not divisible into scalar words");
  }
  const scalars: string[] = [];
  for (let offset = 0; offset < payload.length; offset += 4) {
    const scalar = littleEndian ? payload.readUInt32LE(offset) : payload.readUInt32BE(offset);
    if (scalar > 0x10ffff || (scalar >= 0xd800 && scalar <= 0xdfff)) {
      return sourceError("ENCODING_AMBIGUOUS", "UTF-32 payload contains an invalid Unicode scalar");
    }
    scalars.push(String.fromCodePoint(scalar));
  }
  return scalars.join("");
}

function decodeBytes(payload: Buffer, encoding: ReturnType<typeof normalizeEncoding>): string {
  try {
    if (encoding === "utf-32le") {
      return utf32Decode(payload, true);
    }
    if (encoding === "utf-32be") {
      return utf32Decode(payload, false);
    }
    return new TextDecoder(encoding, { fatal: true, ignoreBOM: true }).decode(payload);
  } catch (error) {
    return sourceError(
      "ENCODING_AMBIGUOUS",
      error instanceof Error ? error.message : `cannot decode ${encoding}`,
    );
  }
}

function decodePlainText(raw: Buffer, explicitEncoding: string | undefined): {
  text: string;
  encoding: string;
  bomLength: number;
  bomPolicy?: string;
} {
  const bom = raw.subarray(0, 4);
  const detected = bom[0] === 0x00 && bom[1] === 0x00 && bom[2] === 0xfe && bom[3] === 0xff
    ? { encoding: "utf-32be" as const, length: 4, policy: "UTF32_BE_BOM" }
    : bom[0] === 0xff && bom[1] === 0xfe && bom[2] === 0x00 && bom[3] === 0x00
      ? { encoding: "utf-32le" as const, length: 4, policy: "UTF32_LE_BOM" }
      : bom[0] === 0xef && bom[1] === 0xbb && bom[2] === 0xbf
        ? { encoding: "utf-8" as const, length: 3, policy: "UTF8_BOM" }
        : bom[0] === 0xfe && bom[1] === 0xff
          ? { encoding: "utf-16be" as const, length: 2, policy: "UTF16_BE_BOM" }
          : bom[0] === 0xff && bom[1] === 0xfe
            ? { encoding: "utf-16le" as const, length: 2, policy: "UTF16_LE_BOM" }
            : undefined;
  const encoding = detected?.encoding ?? (explicitEncoding === undefined ? "utf-8" : normalizeEncoding(explicitEncoding));
  const bomLength = detected?.length ?? 0;
  return {
    text: normalizeNewlines(decodeBytes(raw.subarray(bomLength), encoding)),
    encoding,
    bomLength,
    ...(detected === undefined ? {} : { bomPolicy: detected.policy }),
  };
}

function record(value: unknown, code: SourceImportErrorCode, context: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return sourceError(code, `${context} must be an XML object`);
  }
  return value as Record<string, unknown>;
}

function values(value: unknown): readonly unknown[] {
  return Array.isArray(value) ? value : value === undefined ? [] : [value];
}

function localName(name: string): string {
  return name.includes(":") ? name.slice(name.lastIndexOf(":") + 1) : name;
}

function child(value: Record<string, unknown>, name: string): unknown {
  return Object.entries(value).find(([key]) => localName(key) === name)?.[1];
}

function attribute(value: Record<string, unknown>, name: string): string | undefined {
  const found = Object.entries(value).find(([key]) => key === `@_${name}` || localName(key) === name)?.[1];
  return typeof found === "string" ? found : undefined;
}

function hasDoctypeInternalSubset(xml: string): boolean {
  const declarations = /<!\s*DOCTYPE\b/giu;
  for (let match = declarations.exec(xml); match !== null; match = declarations.exec(xml)) {
    let quote: "\"" | "'" | undefined;
    let terminated = false;
    for (let index = declarations.lastIndex; index < xml.length; index += 1) {
      const character = xml[index]!;
      if (quote !== undefined) {
        if (character === quote) {
          quote = undefined;
        }
        continue;
      }
      if (character === "\"" || character === "'") {
        quote = character;
      } else if (character === "[") {
        return true;
      } else if (character === ">") {
        declarations.lastIndex = index + 1;
        terminated = true;
        break;
      }
    }
    if (!terminated) {
      break;
    }
  }
  return false;
}

function decodeXml(payload: Buffer, code: SourceImportErrorCode, context: string): string {
  let xml: string;
  try {
    xml = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(payload);
  } catch (error) {
    return sourceError(code, error instanceof Error ? error.message : `${context} is not UTF-8 XML`);
  }
  if (/<!\s*ENTITY\b/iu.test(xml)) {
    return sourceError(code, `${context} contains a prohibited XML entity declaration`);
  }
  // EPUB 2 commonly uses a public XHTML DTD. fast-xml-parser never resolves
  // external entities, so that declaration is safe to retain. Internal DTD
  // subsets remain forbidden because they can define custom entities.
  if (hasDoctypeInternalSubset(xml)) {
    return sourceError(code, `${context} contains a prohibited XML DTD internal subset`);
  }
  return xml;
}

function parseXml(payload: Buffer, code: SourceImportErrorCode, context: string): Record<string, unknown> {
  const xml = decodeXml(payload, code, context);
  try {
    return record(XML.parse(xml), code, context);
  } catch (error) {
    return sourceError(code, error instanceof Error ? error.message : `${context} cannot be parsed`);
  }
}

function parseOrderedXml(payload: Buffer, code: SourceImportErrorCode, context: string): OrderedXmlNodes {
  const xml = decodeXml(payload, code, context);
  try {
    const parsed = ORDERED_XML.parse(xml);
    if (!Array.isArray(parsed)) {
      return sourceError(code, `${context} must contain an ordered XML node list`);
    }
    return parsed as OrderedXmlNodes;
  } catch (error) {
    return sourceError(code, error instanceof Error ? error.message : `${context} cannot be parsed`);
  }
}

function orderedElement(node: OrderedXmlNode): { name: string; children: OrderedXmlNodes } | undefined {
  for (const [name, value] of Object.entries(node)) {
    if (name === "#text" || name === ":@") {
      continue;
    }
    if (Array.isArray(value)) {
      return { name, children: value as OrderedXmlNodes };
    }
  }
  return undefined;
}

function firstOrderedElement(nodes: OrderedXmlNodes, targetName: string): OrderedXmlNodes | undefined {
  for (const node of nodes) {
    const element = orderedElement(node);
    if (element === undefined) {
      continue;
    }
    if (localName(element.name) === targetName) {
      return element.children;
    }
    const nested = firstOrderedElement(element.children, targetName);
    if (nested !== undefined) {
      return nested;
    }
  }
  return undefined;
}

function orderedElementsInDocumentOrder(nodes: OrderedXmlNodes, targetName: string): OrderedXmlNodes[] {
  const matches: OrderedXmlNodes[] = [];
  const visit = (current: OrderedXmlNodes): void => {
    for (const node of current) {
      const element = orderedElement(node);
      if (element === undefined) {
        continue;
      }
      if (localName(element.name) === targetName) {
        matches.push(element.children);
        continue;
      }
      visit(element.children);
    }
  };
  visit(nodes);
  return matches;
}

function orderedTextContent(nodes: OrderedXmlNodes): string {
  let text = "";
  for (const node of nodes) {
    const directText = node["#text"];
    if (typeof directText === "string") {
      text += directText;
      continue;
    }
    const element = orderedElement(node);
    if (element === undefined) {
      continue;
    }
    const tag = localName(element.name);
    if (tag === "tab") {
      text += "\t";
    } else if (tag === "br" || tag === "cr") {
      text += "\n";
    } else {
      text += orderedTextContent(element.children);
    }
  }
  return text;
}

function safeArchivePath(value: string): string {
  if (value.length === 0 || value.includes("\\") || value.includes("\0") || value.startsWith("/")
    || value.split("/").some((part) => part === "..")) {
    return sourceError("ARCHIVE_ENTRY_INVALID", `archive member has unsafe path: ${value}`);
  }
  const normalized = posix.normalize(value);
  if (normalized === "." || normalized.startsWith("../") || normalized.startsWith("/")) {
    return sourceError("ARCHIVE_ENTRY_INVALID", `archive member escapes archive root: ${value}`);
  }
  return normalized;
}

function archiveFailure(error: unknown): SourceImportError {
  if (error instanceof SourceImportError) {
    return error;
  }
  const message = error instanceof Error ? error.message : "cannot read archive";
  // yauzl validates some dangerous paths before it yields an entry, so map its
  // own traversal rejection to the same stable error as our entry validator.
  if (/invalid (?:relative )?path|invalid file ?name/iu.test(message)) {
    return new SourceImportError("ARCHIVE_ENTRY_INVALID", message);
  }
  return new SourceImportError(
    "ARCHIVE_INVALID",
    message,
  );
}

function openArchive(raw: Buffer): Promise<Archive> {
  return new Promise((resolveArchive, rejectArchive) => {
    yauzl.fromBuffer(raw, {
      decodeStrings: true,
      lazyEntries: true,
      strictFileNames: false,
      validateEntrySizes: false,
    }, (openError, zip) => {
      if (openError !== null || zip === undefined) {
        rejectArchive(archiveFailure(openError));
        return;
      }
      const entries = new Map<string, Entry>();
      let declaredTotal = 0;
      let settled = false;
      const failArchive = (error: unknown): void => {
        if (settled) {
          return;
        }
        settled = true;
        try {
          zip.close();
        } catch {
          // yauzl can already have closed after a malformed central directory.
        }
        rejectArchive(archiveFailure(error));
      };
      zip.on("error", failArchive);
      zip.on("entry", (entry: Entry) => {
        try {
          const memberPath = safeArchivePath(entry.fileName);
          if (entry.uncompressedSize > MAX_ARCHIVE_ENTRY_BYTES) {
            sourceError("ARCHIVE_ENTRY_TOO_LARGE", `${memberPath} exceeds ${MAX_ARCHIVE_ENTRY_BYTES} bytes`);
          }
          declaredTotal += entry.uncompressedSize;
          if (declaredTotal > MAX_ARCHIVE_EXPANDED_BYTES) {
            sourceError("ARCHIVE_TOO_LARGE", `archive expands beyond ${MAX_ARCHIVE_EXPANDED_BYTES} bytes`);
          }
          if (entries.has(memberPath)) {
            sourceError("ARCHIVE_ENTRY_INVALID", `archive has duplicate member: ${memberPath}`);
          }
          entries.set(memberPath, entry);
          zip.readEntry();
        } catch (error) {
          failArchive(error);
        }
      });
      zip.on("end", () => {
        if (settled) {
          return;
        }
        settled = true;
        resolveArchive({ zip, entries, extractedBytes: 0 });
      });
      zip.readEntry();
    });
  });
}

async function readArchiveMember(
  archive: Archive,
  memberPath: string,
  missingCode: SourceImportErrorCode,
): Promise<Buffer> {
  const safePath = safeArchivePath(memberPath);
  const entry = archive.entries.get(safePath);
  if (entry === undefined || entry.fileName.endsWith("/")) {
    return sourceError(missingCode, `missing archive member: ${safePath}`);
  }
  return new Promise((resolveMember, rejectMember) => {
    archive.zip.openReadStream(entry, (openError, stream) => {
      if (openError !== null || stream === undefined) {
        rejectMember(archiveFailure(openError));
        return;
      }
      const chunks: Buffer[] = [];
      let bytes = 0;
      let settled = false;
      const failMember = (error: unknown): void => {
        if (settled) {
          return;
        }
        settled = true;
        try {
          stream.destroy();
          archive.zip.close();
        } catch {
          // The stream may already be closed by yauzl.
        }
        rejectMember(archiveFailure(error));
      };
      stream.on("data", (chunk: Buffer) => {
        bytes += chunk.length;
        archive.extractedBytes += chunk.length;
        if (bytes > MAX_ARCHIVE_ENTRY_BYTES) {
          failMember(new SourceImportError("ARCHIVE_ENTRY_TOO_LARGE", `${safePath} exceeds entry limit while reading`));
          return;
        }
        if (archive.extractedBytes > MAX_ARCHIVE_EXPANDED_BYTES) {
          failMember(new SourceImportError("ARCHIVE_TOO_LARGE", "archive exceeds total limit while reading"));
          return;
        }
        chunks.push(chunk);
      });
      stream.on("error", failMember);
      stream.on("end", () => {
        if (!settled) {
          settled = true;
          resolveMember(Buffer.concat(chunks));
        }
      });
    });
  });
}

function closeArchive(archive: Archive): void {
  try {
    archive.zip.close();
  } catch {
    // Closing an already closed yauzl reader is harmless for import cleanup.
  }
}

async function extractDocx(raw: Buffer): Promise<ExtractedSource> {
  const archive = await openArchive(raw);
  try {
    const documentXml = await readArchiveMember(archive, "word/document.xml", "DOCX_DOCUMENT_MISSING");
    const orderedDocument = parseOrderedXml(
      documentXml,
      "DOCX_DOCUMENT_INVALID",
      "word/document.xml",
    );
    const body = firstOrderedElement(orderedDocument, "body");
    if (body === undefined) {
      return sourceError("DOCX_DOCUMENT_INVALID", "word/document.xml has no body");
    }
    const paragraphs = orderedElementsInDocumentOrder(body, "p");
    const parts: ExtractedPart[] = paragraphs.map((paragraph, index) => ({
      text: orderedTextContent(paragraph),
      originKind: "docx_paragraph",
      originRef: `word/document.xml#p=${index}`,
      transformation: "xml-text+paragraph-separator",
    }));
    return {
      ...scalarSegments(parts),
      encoding: "zip-container",
      extractor: "docx-paragraph-v1",
      excludedRawRanges: [{ rawStart: 0, rawEnd: raw.length, policy: "DOCX_NON_DOCUMENT_DATA" }],
    };
  } finally {
    closeArchive(archive);
  }
}

function resolveArchiveReference(parentPath: string, reference: string): string {
  const normalizedReference = reference.replaceAll("\\", "/");
  if (normalizedReference.startsWith("/") || normalizedReference.split("/").some((part) => part === "..")) {
    return sourceError("ARCHIVE_ENTRY_INVALID", `archive reference escapes root: ${reference}`);
  }
  return safeArchivePath(posix.normalize(posix.join(posix.dirname(parentPath), normalizedReference)));
}

const XHTML_BLOCK_BOUNDARY = "\u0000folioloom-xhtml-block\u0000";
const XHTML_BLOCK_TAGS = new Set([
  "address", "article", "aside", "blockquote", "caption", "dd", "details", "div", "dl", "dt",
  "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
  "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "tbody", "td",
  "tfoot", "th", "thead", "tr", "ul",
]);
const XHTML_IGNORED_TAGS = new Set(["script", "style", "template"]);

function xhtmlText(document: OrderedXmlNodes): string {
  const body = firstOrderedElement(document, "body");
  if (body === undefined) {
    return "";
  }
  const parts: string[] = [];
  const visit = (nodes: OrderedXmlNodes): void => {
    for (const node of nodes) {
      const directText = node["#text"];
      if (typeof directText === "string") {
        parts.push(directText);
        continue;
      }
      const element = orderedElement(node);
      if (element === undefined) {
        continue;
      }
      const tag = localName(element.name);
      if (XHTML_IGNORED_TAGS.has(tag)) {
        continue;
      }
      if (tag === "br") {
        parts.push("\n");
        continue;
      }
      const isBlock = XHTML_BLOCK_TAGS.has(tag);
      if (isBlock) {
        parts.push(XHTML_BLOCK_BOUNDARY);
      }
      visit(element.children);
      if (isBlock) {
        parts.push(XHTML_BLOCK_BOUNDARY);
      }
    }
  };
  visit(body);
  return normalizeNewlines(parts.join(""))
    .replaceAll(XHTML_BLOCK_BOUNDARY, "\n\n")
    .replace(/\s*\n\n\s*/gu, "\n\n")
    .replace(/\n{3,}/gu, "\n\n")
    .trim();
}

async function extractEpub(raw: Buffer): Promise<ExtractedSource> {
  const archive = await openArchive(raw);
  try {
    const container = parseXml(
      await readArchiveMember(archive, "META-INF/container.xml", "EPUB_CONTAINER_MISSING"),
      "EPUB_CONTAINER_MISSING",
      "META-INF/container.xml",
    );
    const containerRoot = child(container, "container");
    const rootfiles = containerRoot === undefined
      ? undefined
      : child(record(containerRoot, "EPUB_CONTAINER_MISSING", "container"), "rootfiles");
    // An empty <rootfiles/> is a valid container shape but provides no OPF.
    // Treat it as a missing package reference rather than as a corrupt container.
    const rootfilesRecord = typeof rootfiles === "object" && rootfiles !== null && !Array.isArray(rootfiles)
      ? rootfiles as Record<string, unknown>
      : undefined;
    const rootfile = rootfilesRecord === undefined
      ? undefined
      : values(child(rootfilesRecord, "rootfile"))[0];
    const rootfileRecord = typeof rootfile === "object" && rootfile !== null && !Array.isArray(rootfile)
      ? rootfile as Record<string, unknown>
      : undefined;
    const opfPath = rootfileRecord === undefined
      ? undefined
      : attribute(rootfileRecord, "full-path");
    if (opfPath === undefined) {
      return sourceError("EPUB_OPF_MISSING", "container has no rootfile full-path");
    }
    const safeOpfPath = safeArchivePath(opfPath);
    const opf = parseXml(
      await readArchiveMember(archive, safeOpfPath, "EPUB_OPF_MISSING"),
      "EPUB_OPF_MISSING",
      safeOpfPath,
    );
    const packageNode = child(opf, "package");
    if (packageNode === undefined) {
      return sourceError("EPUB_OPF_MISSING", "OPF package node is absent");
    }
    const packageRecord = record(packageNode, "EPUB_OPF_MISSING", "package");
    const manifest = child(packageRecord, "manifest");
    const spine = child(packageRecord, "spine");
    if (manifest === undefined || spine === undefined) {
      return sourceError("EPUB_SPINE_INVALID", "OPF requires manifest and spine");
    }
    const members = new Map<string, string>();
    for (const item of values(child(record(manifest, "EPUB_SPINE_INVALID", "manifest"), "item"))) {
      const itemRecord = record(item, "EPUB_SPINE_INVALID", "manifest item");
      const id = attribute(itemRecord, "id");
      const href = attribute(itemRecord, "href");
      if (id !== undefined && href !== undefined) {
        members.set(id, resolveArchiveReference(safeOpfPath, href));
      }
    }
    const parts: ExtractedPart[] = [];
    for (const itemref of values(child(record(spine, "EPUB_SPINE_INVALID", "spine"), "itemref"))) {
      const idref = attribute(record(itemref, "EPUB_SPINE_INVALID", "spine itemref"), "idref");
      const memberPath = idref === undefined ? undefined : members.get(idref);
      if (memberPath === undefined) {
        return sourceError("EPUB_SPINE_INVALID", `spine reference is not in manifest: ${String(idref)}`);
      }
      const xhtml = parseOrderedXml(
        await readArchiveMember(archive, memberPath, "EPUB_SPINE_INVALID"),
        "EPUB_SPINE_INVALID",
        memberPath,
      );
      parts.push({
        text: xhtmlText(xhtml),
        originKind: "epub_spine_member",
        originRef: memberPath,
        transformation: "xhtml-text+spine-separator",
      });
    }
    if (parts.length === 0) {
      return sourceError("EPUB_SPINE_INVALID", "OPF spine is empty");
    }
    return {
      ...scalarSegments(parts),
      encoding: "zip-container",
      extractor: "epub-spine-v1",
      excludedRawRanges: [{ rawStart: 0, rawEnd: raw.length, policy: "EPUB_NON_SPINE_DATA" }],
    };
  } finally {
    closeArchive(archive);
  }
}

async function extractSource(
  raw: Buffer,
  format: ReturnType<typeof sourceFormat>,
  explicitEncoding: string | undefined,
): Promise<ExtractedSource> {
  if (format === ".docx") {
    return extractDocx(raw);
  }
  if (format === ".epub") {
    return extractEpub(raw);
  }
  const plain = decodePlainText(raw, explicitEncoding);
  return {
    sourceText: plain.text,
    canonicalSegments: [{
      canonicalStart: 0,
      canonicalEnd: scalarLength(plain.text),
      originKind: "decoded_bytes",
      originRef: `source/original${format}`,
      transformation: "decode+newline-normalize",
      rawStart: plain.bomLength,
      rawEnd: raw.length,
    }],
    encoding: plain.encoding,
    extractor: "plain-text-v2",
    excludedRawRanges: plain.bomPolicy === undefined
      ? []
      : [{ rawStart: 0, rawEnd: plain.bomLength, policy: plain.bomPolicy }],
  };
}

async function requireRegularSource(path: string): Promise<string> {
  if (typeof path !== "string" || path.trim().length === 0 || !isAbsolute(path)) {
    return sourceError("SOURCE_INPUT_INVALID", "sourcePath must be an absolute file path");
  }
  let resolved: string;
  try {
    resolved = await realpath(path);
  } catch (error) {
    return sourceError("SOURCE_INPUT_INVALID", error instanceof Error ? error.message : "sourcePath cannot be resolved");
  }
  try {
    if (!(await stat(resolved)).isFile()) {
      return sourceError("SOURCE_INPUT_INVALID", "sourcePath must identify a regular file");
    }
  } catch (error) {
    return sourceError("SOURCE_INPUT_INVALID", error instanceof Error ? error.message : "sourcePath cannot be inspected");
  }
  return resolved;
}

function requireProjectDirectory(path: string): string {
  if (typeof path !== "string" || path.trim().length === 0 || !isAbsolute(path)) {
    return sourceError("SOURCE_INPUT_INVALID", "projectDirectory must be an absolute directory path");
  }
  return resolve(path);
}

function pathWithin(parent: string, candidate: string): boolean {
  const child = relative(parent, candidate);
  return child.length > 0 && child !== ".." && !child.startsWith(`..${sep}`) && !isAbsolute(child);
}

function sourceLanguage(requested: string, detected: DetectedLanguage | undefined): string {
  if (requested.trim().toLocaleLowerCase("en") === "auto") {
    return detected?.id ?? "und";
  }
  if (requested.trim().length === 0) {
    return sourceError("SOURCE_INPUT_INVALID", "sourceLanguage must be a language id or auto");
  }
  return requested;
}

export class SourceImporter {
  readonly #readSource: (sourcePath: string) => Promise<Buffer>;

  constructor(dependencies: SourceImporterDependencies = {}) {
    this.#readSource = dependencies.readSource ?? readFile;
  }

  async importSource(request: SourceImportRequest): Promise<SourceImportResult> {
    const sourcePath = await requireRegularSource(request.sourcePath);
    const projectDirectory = requireProjectDirectory(request.projectDirectory);
    const format = sourceFormat(sourcePath);
    const raw = await this.#readSource(sourcePath);
    const rawSha256 = sha256(raw);
    const extracted = await extractSource(raw, format, request.explicitEncoding);
    const detectedLanguage = detectLanguage(extracted.sourceText);
    const language = sourceLanguage(request.sourceLanguage, detectedLanguage);
    const parent = dirname(projectDirectory);
    await mkdir(parent, { recursive: true });
    if ((await stat(projectDirectory).catch(() => undefined)) !== undefined) {
      return sourceError("PROJECT_EXISTS", `project directory already exists: ${projectDirectory}`);
    }
    const temporaryDirectory = join(parent, `.folioloom-import-${randomUUID()}`);
    if (!pathWithin(parent, temporaryDirectory)) {
      return sourceError("SOURCE_INPUT_INVALID", "temporary project directory escapes parent");
    }
    const rawMember = `source/original${format}`;
    const canonicalMember = "source.txt";
    try {
      await mkdir(join(temporaryDirectory, "source"), { recursive: true });
      await writeFile(join(temporaryDirectory, rawMember), raw);
      await writeFile(join(temporaryDirectory, canonicalMember), extracted.sourceText, "utf8");
      const manifest = {
        schema_version: "v5-source-ledger-1",
        coordinate_unit: "unicode_scalar",
        raw_path: rawMember,
        raw_size: raw.length,
        raw_sha256: rawSha256,
        source_format: format,
        encoding: extracted.encoding,
        extractor: extracted.extractor,
        sourceLanguage: language,
        canonical_path: canonicalMember,
        canonical_chars: scalarLength(extracted.sourceText),
        canonical_sha256: sha256(Buffer.from(extracted.sourceText, "utf8")),
        canonical_segments: extracted.canonicalSegments.map((segment) => ({
          canonical_start: segment.canonicalStart,
          canonical_end: segment.canonicalEnd,
          origin_kind: segment.originKind,
          origin_ref: segment.originRef,
          transformation: segment.transformation,
          ...(segment.rawStart === undefined ? {} : { raw_start: segment.rawStart }),
          ...(segment.rawEnd === undefined ? {} : { raw_end: segment.rawEnd }),
        })),
        excluded_raw_ranges: extracted.excludedRawRanges.map((range) => ({
          raw_start: range.rawStart,
          raw_end: range.rawEnd,
          policy: range.policy,
        })),
      };
      await writeFile(join(temporaryDirectory, "source_manifest.json"), JSON.stringify(manifest), "utf8");
      SourceLedger.open(join(temporaryDirectory, "source_manifest.json"));
      const verifiedRaw = await this.#readSource(sourcePath);
      if (sha256(verifiedRaw) !== rawSha256) {
        return sourceError("SOURCE_CHANGED_DURING_IMPORT", "source bytes changed while importing");
      }
      await rename(temporaryDirectory, projectDirectory);
      return {
        manifestPath: join(projectDirectory, "source_manifest.json"),
        rawSha256,
        canonicalChars: scalarLength(extracted.sourceText),
        ...(detectedLanguage === undefined ? {} : { detectedLanguage }),
      };
    } catch (error) {
      await rm(temporaryDirectory, { recursive: true, force: true });
      if (error instanceof SourceImportError) {
        throw error;
      }
      throw error;
    }
  }
}

const defaultImporter = new SourceImporter();

export function importSource(request: SourceImportRequest): Promise<SourceImportResult> {
  return defaultImporter.importSource(request);
}
