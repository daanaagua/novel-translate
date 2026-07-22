import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";

import type { DesktopProjectRequest, DesktopRecentProject } from "./contracts.js";

const PREFERENCES_SCHEMA = "folioloom-desktop-preferences-1";

function record(value: unknown): Record<string, unknown> | undefined {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return undefined;
  }
  return value as Record<string, unknown>;
}

function absolutePath(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 && isAbsolute(value)
    ? value
    : undefined;
}

function runId(value: unknown): string | undefined {
  return typeof value === "string" && value.trim().length > 0
    ? value
    : undefined;
}

function recentProject(value: unknown): DesktopRecentProject | undefined {
  const candidate = record(value);
  if (candidate === undefined) {
    return undefined;
  }
  const manifestPath = absolutePath(candidate.manifestPath);
  if (manifestPath === undefined) {
    return undefined;
  }
  const storePath = candidate.storePath === undefined
    ? undefined
    : absolutePath(candidate.storePath);
  if (candidate.storePath !== undefined && storePath === undefined) {
    return undefined;
  }
  const selectedRunId = candidate.runId === undefined ? undefined : runId(candidate.runId);
  if (candidate.runId !== undefined && selectedRunId === undefined) {
    return undefined;
  }
  return {
    manifestPath,
    ...(storePath === undefined ? {} : { storePath }),
    ...(selectedRunId === undefined ? {} : { runId: selectedRunId }),
  };
}

function storedRecentProject(request: DesktopProjectRequest): DesktopRecentProject {
  const recent = recentProject({
    manifestPath: request.manifestPath,
    ...(request.storePath === undefined ? {} : { storePath: request.storePath }),
    ...(request.runId === undefined ? {} : { runId: request.runId }),
  });
  if (recent === undefined) {
    throw new TypeError("recent project requires an absolute manifestPath and valid optional fields");
  }
  return recent;
}

export class DesktopPreferences {
  readonly #path: string;

  constructor(path: string) {
    this.#path = resolve(path);
  }

  load(): DesktopRecentProject | undefined {
    try {
      const payload = record(JSON.parse(readFileSync(this.#path, "utf8")) as unknown);
      if (payload?.schema !== PREFERENCES_SCHEMA) {
        return undefined;
      }
      return payload.recent === undefined ? undefined : recentProject(payload.recent);
    } catch {
      return undefined;
    }
  }

  save(request: DesktopProjectRequest): void {
    const recent = storedRecentProject(request);
    const parent = dirname(this.#path);
    mkdirSync(parent, { recursive: true });
    const temporaryPath = `${this.#path}.${randomUUID()}.tmp`;
    try {
      writeFileSync(temporaryPath, JSON.stringify({
        schema: PREFERENCES_SCHEMA,
        recent,
      }), "utf8");
      renameSync(temporaryPath, this.#path);
    } finally {
      rmSync(temporaryPath, { force: true });
    }
  }
}
