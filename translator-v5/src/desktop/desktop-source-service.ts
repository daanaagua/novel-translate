import { createHash } from "node:crypto";
import type { Dirent } from "node:fs";
import { mkdir, readFile, readdir, realpath, rm, stat } from "node:fs/promises";
import { basename, extname, isAbsolute, join, resolve } from "node:path";

import {
  importSource,
  SourceImportError,
  type SourceImportResult,
} from "../source/source-importer.js";
import { SourceLedger } from "../source/source-ledger.js";

export interface DesktopSourceServiceOptions {
  projectsRoot: string;
}

export interface DesktopSourceImportRequest {
  sourcePath: string;
  sourceLanguage?: string;
  explicitEncoding?: string;
}

export interface DesktopSourceImportResult extends SourceImportResult {
  projectDirectory: string;
  reused: boolean;
}

function sha256(payload: Buffer): string {
  return createHash("sha256").update(payload).digest("hex");
}

function sanitizeTitle(sourcePath: string): string {
  const fileName = basename(sourcePath);
  const title = fileName.slice(0, Math.max(0, fileName.length - extname(fileName).length))
    .normalize("NFKC")
    .replace(/[<>:"/\\|?*\u0000-\u001f]+/gu, " ")
    .replace(/\s+/gu, " ")
    .trim()
    .replace(/[ .-]+/gu, "-")
    .replace(/^-+|-+$/gu, "");
  return title.length === 0 ? "Untitled" : title.slice(0, 80);
}

function projectName(sourcePath: string, rawSha256: string): string {
  return `${sanitizeTitle(sourcePath)}-${rawSha256.slice(0, 12)}`;
}

async function regularAbsoluteFile(path: string): Promise<string> {
  if (typeof path !== "string" || path.trim().length === 0 || !isAbsolute(path)) {
    throw new SourceImportError("SOURCE_INPUT_INVALID", "sourcePath must be an absolute file path");
  }
  let resolved: string;
  try {
    resolved = await realpath(path);
    if (!(await stat(resolved)).isFile()) {
      throw new SourceImportError("SOURCE_INPUT_INVALID", "sourcePath must identify a regular file");
    }
  } catch (error) {
    if (error instanceof SourceImportError) {
      throw error;
    }
    throw new SourceImportError(
      "SOURCE_INPUT_INVALID",
      error instanceof Error ? error.message : "sourcePath cannot be resolved",
    );
  }
  return resolved;
}

async function existingProjectForHash(
  projectsRoot: string,
  rawSha256: string,
): Promise<DesktopSourceImportResult | undefined> {
  let entries: Dirent<string>[];
  try {
    entries = await readdir(projectsRoot, { withFileTypes: true });
  } catch {
    return undefined;
  }
  for (const entry of entries) {
    if (!entry.isDirectory()) {
      continue;
    }
    const projectDirectory = join(projectsRoot, entry.name);
    const manifestPath = join(projectDirectory, "source_manifest.json");
    try {
      const ledger = SourceLedger.open(manifestPath);
      if (sha256(await readFile(ledger.rawPath)) !== rawSha256) {
        continue;
      }
      return {
        projectDirectory,
        manifestPath: ledger.manifestPath,
        rawSha256,
        canonicalChars: ledger.canonicalChars,
        reused: true,
      };
    } catch {
      // A broken or unrelated directory must not prevent importing a new manuscript.
    }
  }
  return undefined;
}

async function unusedProjectDirectory(projectsRoot: string, baseName: string): Promise<string> {
  for (let suffix = 0; ; suffix += 1) {
    const candidate = join(projectsRoot, suffix === 0 ? baseName : `${baseName}-${suffix + 1}`);
    try {
      await stat(candidate);
    } catch {
      return candidate;
    }
  }
}

export class DesktopSourceService {
  readonly #projectsRoot: string;

  constructor(options: DesktopSourceServiceOptions) {
    if (!isAbsolute(options.projectsRoot)) {
      throw new SourceImportError("SOURCE_INPUT_INVALID", "projectsRoot must be an absolute directory path");
    }
    this.#projectsRoot = resolve(options.projectsRoot);
  }

  async importSource(request: DesktopSourceImportRequest): Promise<DesktopSourceImportResult> {
    const sourcePath = await regularAbsoluteFile(request.sourcePath);
    const initialRawSha256 = sha256(await readFile(sourcePath));
    await mkdir(this.#projectsRoot, { recursive: true });
    const existing = await existingProjectForHash(this.#projectsRoot, initialRawSha256);
    if (existing !== undefined) {
      return existing;
    }
    const projectDirectory = await unusedProjectDirectory(
      this.#projectsRoot,
      projectName(sourcePath, initialRawSha256),
    );
    const imported = await importSource({
      sourcePath,
      projectDirectory,
      sourceLanguage: request.sourceLanguage ?? "auto",
      ...(request.explicitEncoding === undefined ? {} : { explicitEncoding: request.explicitEncoding }),
    });
    if (imported.rawSha256 !== initialRawSha256) {
      await rm(projectDirectory, { recursive: true, force: true });
      throw new SourceImportError("SOURCE_CHANGED_DURING_IMPORT", "source bytes changed before project import completed");
    }
    return { ...imported, projectDirectory, reused: false };
  }
}
