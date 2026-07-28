import assert from "node:assert/strict";
import test from "node:test";

import {
  AdmissionController,
  BookTokenEnvelopeExceededError,
} from "../src/fullbook/admission-controller.js";
import { TokenLedger } from "../src/fullbook/token-ledger.js";

function controller(mode: "active" | "shadow" | "off" = "active") {
  const events: unknown[] = [];
  const ledger = TokenLedger.create({
    mode,
    profile: "balanced",
    tokenIncreaseCap: 0.1,
  });
  const admission = new AdmissionController({
    ledger,
    mode,
    persist(event) {
      events.push(event);
      ledger.apply(event);
    },
  });
  return { admission, ledger, events };
}

test("admission reserves and settles through the ledger", () => {
  const { admission, ledger } = controller();
  admission.addBaseline({
    taskIds: ["t1"],
    baselineTokens: 1000,
    source: "translate_horizon",
    reason: "wave",
  });
  assert.equal(admission.canLaunch(100), true);
  admission.reserve({
    requestId: "r1",
    purpose: "translate",
    taskIds: ["t1"],
    predictedTokens: 100,
    attempt: 0,
  });
  assert.equal(ledger.state().reservedTokens, 100);
  admission.settle({
    requestId: "r1",
    actualTokens: 120,
    usageComplete: true,
    outcome: "success",
  });
  assert.equal(ledger.state().spentTokens, 120);
  assert.equal(ledger.state().reservedTokens, 0);
});

test("active mode rejects launch that exceeds envelope", () => {
  const { admission } = controller("active");
  admission.addBaseline({
    taskIds: ["t1"],
    baselineTokens: 100,
    source: "translate_horizon",
    reason: "wave",
  });
  assert.equal(admission.canLaunch(111), false);
  assert.throws(
    () => admission.reserve({
      requestId: "r1",
      purpose: "translate",
      taskIds: ["t1"],
      predictedTokens: 111,
      attempt: 0,
    }),
    (error: unknown) => error instanceof BookTokenEnvelopeExceededError,
  );
});

test("shadow mode still records spend but does not hard-reject", () => {
  const { admission, ledger } = controller("shadow");
  admission.addBaseline({
    taskIds: ["t1"],
    baselineTokens: 100,
    source: "translate_horizon",
    reason: "wave",
  });
  assert.equal(admission.canLaunch(200), true);
  admission.reserve({
    requestId: "r1",
    purpose: "translate",
    taskIds: ["t1"],
    predictedTokens: 200,
    attempt: 0,
  });
  admission.settle({
    requestId: "r1",
    actualTokens: 250,
    usageComplete: true,
    outcome: "success",
  });
  assert.equal(ledger.state().spentTokens, 250);
});

test("holdSecondary releases without spending", () => {
  const { admission, ledger } = controller("active");
  admission.addBaseline({
    taskIds: ["t1"],
    baselineTokens: 1000,
    source: "translate_horizon",
    reason: "wave",
  });
  admission.reserve({
    requestId: "parent",
    purpose: "translate",
    taskIds: ["t1"],
    predictedTokens: 100,
    attempt: 0,
  });
  const hold = admission.holdSecondary({
    requestId: "parent:protocol:0",
    purpose: "protocol_switch",
    taskIds: ["t1"],
    predictedTokens: 80,
    attempt: 0,
  });
  assert.equal(ledger.state().reservedTokens, 180);
  hold.release();
  assert.equal(ledger.state().reservedTokens, 100);
  assert.equal(ledger.state().spentTokens, 0);
});
