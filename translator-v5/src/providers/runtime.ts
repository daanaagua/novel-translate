import type { StreamFn } from "@earendil-works/pi-agent-core";
import type { Api, Model, OpenAICompletionsCompat, OpenAIResponsesCompat } from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";
import { openAIResponsesApi } from "@earendil-works/pi-ai/api/openai-responses.lazy";

import { providerRegistry, toInternalThinking } from "./registry.js";
import type {
  ModelProfile,
  ProviderRuntime,
  ResolvedProviderProfile,
  SecretCredential,
} from "./types.js";

export interface ProviderRuntimeOptions {
  trustedBaseUrl?: string;
  timeoutMs?: number;
}

function requireCredential(value: SecretCredential): SecretCredential {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new TypeError("provider credential must be a non-empty string");
  }
  return value;
}

function thinkingMap(resolved: ResolvedProviderProfile): Record<string, string | null> | undefined {
  if (resolved.definition.capabilities.thinkingFormat === "qwen") {
    return undefined;
  }
  const effort = resolved.profile.reasoningEffort;
  const internal = effort === undefined ? undefined : toInternalThinking(effort);
  if (internal === undefined || effort === undefined) {
    return undefined;
  }
  return { [internal]: effort };
}

function chatCompat(resolved: ResolvedProviderProfile): OpenAICompletionsCompat {
  const capabilities = resolved.definition.capabilities;
  return {
    thinkingFormat: capabilities.thinkingFormat,
    supportsReasoningEffort: capabilities.reasoning && capabilities.thinkingFormat !== "qwen",
    requiresReasoningContentOnAssistantMessages: capabilities.requiresReasoningContentOnAssistantMessages,
    supportsStrictMode: true,
    ...(resolved.definition.id === "kimi-cn" ? { deferredToolsMode: "kimi" as const } : {}),
  };
}

function createModel(resolved: ResolvedProviderProfile, baseUrl: string): Model<Api> {
  const capabilities = resolved.definition.capabilities;
  const common = {
    id: resolved.profile.modelId,
    name: resolved.profile.modelId,
    provider: `folioloom-${resolved.definition.id}`,
    baseUrl,
    reasoning: capabilities.reasoning,
    input: ["text"] as ("text" | "image")[],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: capabilities.contextWindow,
    maxTokens: capabilities.maxTokens,
    thinkingLevelMap: thinkingMap(resolved),
  };
  if (resolved.definition.apiFamily === "openai-responses") {
    const model: Model<"openai-responses"> = {
      ...common,
      api: "openai-responses",
      compat: {
        supportsDeveloperRole: true,
      } satisfies OpenAIResponsesCompat,
    };
    return model;
  }
  const model: Model<"openai-completions"> = {
    ...common,
    api: "openai-completions",
    compat: chatCompat(resolved),
  };
  return model;
}

export function createProviderRuntime(
  profile: ModelProfile,
  credential: SecretCredential,
  options: ProviderRuntimeOptions = {},
): ProviderRuntime {
  const resolved = providerRegistry.resolve(profile);
  const apiKey = requireCredential(credential);
  const baseUrl = options.trustedBaseUrl === undefined
    ? resolved.baseUrl
    : options.trustedBaseUrl.trim().replace(/\/$/, "");
  if (baseUrl.length === 0) {
    throw new TypeError("trusted provider base URL must be a non-empty string");
  }
  const model = createModel(resolved, baseUrl);
  const internalThinking = profile.reasoningEffort === undefined
    ? undefined
    : toInternalThinking(profile.reasoningEffort);
  const api = resolved.definition.apiFamily === "openai-responses"
    ? openAIResponsesApi()
    : openAICompletionsApi();
  const streamFn: StreamFn = (streamModel, context, streamOptions) => api.streamSimple(streamModel, context, {
    ...streamOptions,
    apiKey,
    ...(options.timeoutMs === undefined ? {} : { timeoutMs: options.timeoutMs }),
    ...(internalThinking === undefined ? {} : { reasoning: internalThinking }),
  });
  return { model, streamFn };
}
