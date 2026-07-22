import type { DesktopError, DesktopResult } from "./contracts.js";
import { SourceIntegrityError } from "../source/source-ledger.js";

export class DesktopInputError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "DesktopInputError";
    this.code = code;
  }
}

export function ok<T>(value: T): DesktopResult<T> {
  return { ok: true, value };
}

export function fail<T = never>(error: DesktopError): DesktopResult<T> {
  return { ok: false, error };
}

export function toDesktopError(error: unknown): DesktopError {
  if (error instanceof SourceIntegrityError || error instanceof DesktopInputError) {
    return { code: error.code, message: error.message, retryable: false };
  }
  const structured = error !== null && typeof error === "object"
    ? error as { code?: unknown }
    : {};
  return {
    code: typeof structured.code === "string" && structured.code.trim().length > 0
      ? structured.code
      : "DESKTOP_ERROR",
    message: error instanceof Error ? error.message : String(error),
    retryable: false,
  };
}
