import assert from "node:assert/strict";
import test from "node:test";

import { getGuidedProgress } from "../app/guided-progress.ts";


test("starts with the AI connection as the only current step", () => {
  const progress = getGuidedProgress({
    aiConnected: false,
    verifiedProfileFacts: 0,
    targetOpportunities: 0,
    hasGroundedFit: false,
  });

  assert.equal(progress.completedCount, 0);
  assert.equal(progress.nextStep, "ai-connection");
  assert.deepEqual(
    progress.steps.map((step) => [step.id, step.complete, step.current]),
    [
      ["ai-connection", false, true],
      ["reviewed-profile", false, false],
      ["target-opportunity", false, false],
      ["grounded-fit", false, false],
    ],
  );
});

test("requires a confirmed fact before treating a profile as reviewed", () => {
  const progress = getGuidedProgress({
    aiConnected: true,
    verifiedProfileFacts: 0,
    targetOpportunities: 3,
    hasGroundedFit: false,
  });

  assert.equal(progress.completedCount, 2);
  assert.equal(progress.nextStep, "reviewed-profile");
  assert.equal(
    progress.steps.find((step) => step.id === "target-opportunity")?.complete,
    true,
  );
});

test("marks the guided start complete only when all four API-backed states exist", () => {
  const progress = getGuidedProgress({
    aiConnected: true,
    verifiedProfileFacts: 4,
    targetOpportunities: 1,
    hasGroundedFit: true,
  });

  assert.equal(progress.completedCount, 4);
  assert.equal(progress.nextStep, null);
  assert.ok(progress.steps.every((step) => step.complete && !step.current));
});
