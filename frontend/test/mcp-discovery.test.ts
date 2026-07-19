import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  createMCPProbeCompletionGuard,
  reconcileMCPToolDraft,
} from "../app/mcp-discovery.ts";
import {
  isMCPProbeIntent,
  isMCPProbeResult,
} from "../app/settings-recovery.ts";


test("late probe completions are suppressed after selection or lifecycle invalidation", () => {
  const guard = createMCPProbeCompletionGuard();
  const first = guard.begin("server-a");
  const effects: string[] = [];

  guard.invalidate();
  assert.equal(guard.commit(first, () => effects.push("stale")), false);

  const current = guard.begin("server-b");
  assert.equal(guard.commit(current, () => effects.push("current")), true);
  assert.deepEqual(effects, ["current"]);
});

test("tool reconciliation changes only the draft exact-name list", () => {
  const added = reconcileMCPToolDraft("search_jobs\nget_job", "search_company", true);
  assert.equal(added, "search_jobs\nget_job\nsearch_company");
  assert.equal(
    reconcileMCPToolDraft(added, "search_jobs", false),
    "get_job\nsearch_company",
  );
  assert.equal(
    reconcileMCPToolDraft("search_jobs, search_jobs", "search_jobs", true),
    "search_jobs",
  );
});

test("probe responses are runtime-validated before UI state changes", () => {
  const intent = {
    approval_id: "00000000-0000-0000-0000-000000000000",
    expires_at: "2026-07-19T12:00:00Z",
    disclosure: {
      transport: "stdio",
      target: "/usr/bin/example",
      target_label: "Command summary",
      bound_target_note: "The exact saved command and 1 saved argument is bound.",
      operations: [
        "MCP initialize",
        "MCP initialized notification",
        "MCP tools/list (up to 4 paginated requests)",
      ],
      risk: "This can run local code.",
      timeout_seconds: 10,
      launches_subprocess: true,
      network_possible: true,
      configuration_will_change: false,
      server_side_effects_possible: true,
    },
  };
  assert.equal(isMCPProbeIntent(intent), true);
  assert.equal(isMCPProbeIntent({ ...intent, disclosure: { ...intent.disclosure, operations: ["MCP tools/call"] } }), false);

  const result = {
    executed: true,
    status: "ready",
    message: "Connection ready.",
    checked_at: "2026-07-19T12:00:01Z",
    latency_ms: 4,
    truncated: false,
    stale_configuration: false,
    discovered_tools: [{
      name: "search_jobs",
      description: "Untrusted claim.",
      allowed: false,
      selectable: true,
      policy_reason: null,
    }],
    allowed_present: [],
    allowed_missing: [],
    discovered_not_allowed: ["search_jobs"],
  };
  assert.equal(isMCPProbeResult(result), true);
  assert.equal(isMCPProbeResult({ ...result, checked_at: null }), false);
});

test("the discovery UI exposes the bounded review, explicit groups, and untrusted labels", () => {
  const componentPath = fileURLToPath(new URL("../app/mcp-settings.tsx", import.meta.url));
  const source = readFileSync(componentPath, "utf8");

  assert.match(source, /Review what CareerPilot will do/);
  assert.match(source, /bounded request counts/);
  assert.match(source, /bound_target_note/);
  assert.match(source, /isMCPProbeIntent\(payload\)/);
  assert.match(source, /isMCPProbeResult\(payload\)/);
  assert.doesNotMatch(source, />Saved target</);
  assert.match(source, /No tool calls, prompts, resources, OAuth flow, retries, or background checks/);
  assert.match(source, /Allowed and present/);
  assert.match(source, /New tools available/);
  assert.match(source, /Allowed but missing/);
  assert.match(source, /Untrusted server-reported description/);
  assert.match(source, /checked_at: checkedAt/);
  assert.match(source, /probeAbortRef\.current\?\.abort\(\)/);
  assert.match(source, /querySelectorAll<HTMLButtonElement>\("button:not\(:disabled\)"\)/);
  assert.match(source, /<button autoFocus className="secondary-action"/);
  assert.match(source, /probeTriggerRef\.current\?\.focus\(\)/);
  assert.doesNotMatch(source, /checked_at: new Date\(\)\.toISOString\(\)/);
});
