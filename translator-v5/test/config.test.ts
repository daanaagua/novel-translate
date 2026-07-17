import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { loadPilotConfig } from "../src/config.js";

const fixturePath = fileURLToPath(
  new URL("./fixtures/config.yaml", import.meta.url),
);

test("loads the selected DeepSeek role without exposing the key in JSON", () => {
  const config = loadPilotConfig(fixturePath, "draft");

  assert.equal(config.provider, "deepseek");
  assert.equal(config.model, "deepseek-v4-flash");
  assert.equal(config.reasoningEffort, "high");
  assert.equal(JSON.stringify(config).includes("secret-test-key"), false);
  assert.equal(config.apiKeyForRuntime(), "secret-test-key");
});
