import assert from "node:assert/strict";
import test from "node:test";

import {
  fauxAssistantMessage,
  fauxProvider,
  fauxToolCall,
} from "@earendil-works/pi-ai";

import {
  createDeepSeekModel,
  PiRuntime,
} from "../src/agents/pi-runtime.js";
import { BudgetLedger } from "../src/kernel/budget.js";
import { Type, type TypedToolSpec } from "../src/tools/tool-spec.js";

function streamFrom(faux: ReturnType<typeof fauxProvider>) {
  return faux.provider.streamSimple.bind(faux.provider);
}

test("Pi executes an allowlisted tool and stops on terminating submit", async () => {
  const faux = fauxProvider();
  const executed: string[] = [];
  const tools: TypedToolSpec[] = [
    {
      name: "search_mentions",
      label: "Search mentions",
      description: "Fixture search.",
      phase: "research",
      parameters: Type.Object({ subjectIds: Type.Array(Type.String()) }),
      execute: async () => {
        executed.push("search_mentions");
        return { hits: [] };
      },
    },
    {
      name: "finish_research",
      label: "Finish research",
      description: "Fixture termination.",
      phase: "research",
      parameters: Type.Object({ unresolvedQuestionIds: Type.Array(Type.String()) }),
      execute: async () => {
        executed.push("finish_research");
        return { finished: true };
      },
    },
  ];
  faux.setResponses([
    fauxAssistantMessage(
      fauxToolCall("search_mentions", { subjectIds: ["typhon"] }),
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage(
      fauxToolCall("finish_research", { unresolvedQuestionIds: [] }),
      { stopReason: "toolUse" },
    ),
  ]);
  const runtime = new PiRuntime();
  const result = await runtime.run({
      systemPrompt: "Use the tools.",
      prompt: "Research Typhon.",
      phase: "research",
      model: faux.getModel(),
      tools,
      budget: new BudgetLedger(),
      terminateTools: ["finish_research"],
  }, streamFrom(faux));

  assert.deepEqual(executed, ["search_mentions", "finish_research"]);
  assert.deepEqual(result.toolNames, ["search_mentions", "finish_research"]);
  assert.equal(result.modelCalls, 2);
  assert.equal(result.stopReason, "toolUse");
  assert.ok(result.usage.totalTokens > 0);
});

test("Pi never executes a tool outside its capability registry", async () => {
  const faux = fauxProvider();
  let executed = 0;
  const tools: TypedToolSpec[] = [{
    name: "finish_research",
    label: "Finish research",
    description: "Only legal fixture capability.",
    phase: "research",
    parameters: Type.Object({ unresolvedQuestionIds: Type.Array(Type.String()) }),
    execute: async () => {
      executed += 1;
      return { finished: true };
    },
  }];
  faux.setResponses([
    fauxAssistantMessage(
      fauxToolCall("shell", { command: "whoami" }),
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage("Stopped after the rejected tool."),
  ]);
  const result = await new PiRuntime().run({
      systemPrompt: "Never use arbitrary tools.",
      prompt: "Try the fixture.",
      phase: "research",
      model: faux.getModel(),
      tools,
      budget: new BudgetLedger(),
      terminateTools: ["finish_research"],
  }, streamFrom(faux));

  assert.equal(executed, 0);
  assert.ok(result.toolErrors.some((item) => item.toolName === "shell"));
});

test("Pi refuses a pre-aborted session before a model call", async () => {
  const faux = fauxProvider();
  const controller = new AbortController();
  controller.abort(new Error("fixture abort"));
  await assert.rejects(
    new PiRuntime().run({
        systemPrompt: "Stop.",
        prompt: "Do not run.",
        phase: "research",
        model: faux.getModel(),
        tools: [],
        budget: new BudgetLedger(),
        signal: controller.signal,
    }, streamFrom(faux)),
    /fixture abort|aborted/i,
  );
  assert.equal(faux.state.callCount, 0);
});

test("DeepSeek model metadata carries compatibility controls but no API key", () => {
  const secret = "fixture-secret-must-not-serialize";
  const model = createDeepSeekModel({
    provider: "deepseek",
    model: "deepseek-v4-flash",
    baseUrl: "https://example.invalid/v1",
    timeoutMs: 30_000,
    reasoningEffort: "high",
    apiKeyForRuntime: () => secret,
    toJSON: () => ({}),
  });

  assert.equal(model.maxTokens, 37_200);
  assert.equal(model.compat?.thinkingFormat, "deepseek");
  assert.equal(model.compat?.requiresReasoningContentOnAssistantMessages, true);
  assert.equal(JSON.stringify(model).includes(secret), false);
});
