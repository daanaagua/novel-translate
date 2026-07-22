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

interface PublicErrorDefinition {
  message: string;
  nextAction?: string;
  retryable: boolean;
}

const PUBLIC_ERRORS: Readonly<Record<string, PublicErrorDefinition>> = Object.freeze({
  AUTH_INVALID: {
    message: "API Key 无效或已失效",
    nextAction: "请检查密钥是否完整，并确认它属于当前选择的服务商。",
    retryable: false,
  },
  MODEL_NOT_FOUND: {
    message: "没有找到这个模型",
    nextAction: "请核对模型 ID，或重新获取模型列表。",
    retryable: false,
  },
  QUOTA_EXHAUSTED: {
    message: "账户额度不足",
    nextAction: "请在服务商控制台检查余额或配额。",
    retryable: false,
  },
  RATE_LIMITED: {
    message: "请求过于频繁",
    nextAction: "请稍后再试，或在服务商控制台检查限流设置。",
    retryable: true,
  },
  PROVIDER_BUSY: {
    message: "模型服务暂时繁忙",
    nextAction: "请稍后重试。",
    retryable: true,
  },
  TOOL_CALL_UNSUPPORTED: {
    message: "这个模型不能完成 FolioLoom 所需的工具调用",
    nextAction: "请换用支持工具调用的模型。",
    retryable: false,
  },
  REASONING_CONTINUITY_UNSUPPORTED: {
    message: "这个模型不能稳定继续多轮推理",
    nextAction: "请换用兼容性检查通过的模型。",
    retryable: false,
  },
  REQUEST_TIMEOUT: {
    message: "连接模型服务超时",
    nextAction: "请检查网络后重试。",
    retryable: true,
  },
  ENCODING_AMBIGUOUS: {
    message: "无法确定书稿的文字编码",
    nextAction: "请重新选择正确的编码后导入。",
    retryable: false,
  },
});

export function redactDesktopTechnicalDetails(value: string): string {
  return value
    .replace(/Authorization\s*:\s*Bearer\s+[^\s;,]+/giu, "Authorization: Bearer [REDACTED]")
    .replace(/\b(?:api[_-]?key|access[_-]?token|token)\s*[:=]\s*[^\s;,]+/giu, "credential=[REDACTED]")
    .replace(/https?:\/\/[^\s;,?#]+\?[^\s;,]*/giu, (url) => url.slice(0, url.indexOf("?")))
    .replace(/\bsk-[A-Za-z0-9_-]{8,}\b/gu, "[REDACTED]")
    .slice(0, 2_000);
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
  const code = typeof structured.code === "string" && structured.code.trim().length > 0
    ? structured.code
    : "DESKTOP_ERROR";
  const details = redactDesktopTechnicalDetails(error instanceof Error ? error.message : String(error));
  const definition = PUBLIC_ERRORS[code];
  if (definition !== undefined) {
    return {
      code,
      message: definition.message,
      ...(definition.nextAction === undefined ? {} : { nextAction: definition.nextAction }),
      retryable: definition.retryable,
      ...(details.length === 0 ? {} : { technicalDetails: details }),
    };
  }
  return {
    code,
    message: "操作没有完成",
    retryable: false,
    ...(details.length === 0 ? {} : { technicalDetails: details }),
  };
}
