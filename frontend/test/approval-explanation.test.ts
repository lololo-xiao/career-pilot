import assert from "node:assert/strict";
import test from "node:test";

import { explainApproval } from "../app/approval-explanation.ts";


test("summarizes a read-only Greenhouse and Lever job search", () => {
  const explanation = explainApproval({
    tool: "execute_code",
    description: "The code runner requires approval.",
    command: `execute_code <<'PY'
import urllib.request
greenhouse = "https://boards-api.greenhouse.io/v1/boards/mistral/jobs"
lever = "https://api.lever.co/v0/postings/alan"
with urllib.request.urlopen(greenhouse) as response:
    print(response.read())
PY`,
  });

  assert.equal(explanation.summary, "Search selected company career pages for open roles.");
  assert.equal(explanation.tool, "Python code runner");
  assert.match(explanation.behavior, /Reads public listings/);
  assert.match(explanation.reason, /appears read-only/);
  assert.equal(explanation.scope, "One run only");
});

test("does not describe outbound writes as read-only", () => {
  const explanation = explainApproval({
    tool: "execute_code",
    description: "The code runner requires approval.",
    command: `import requests
requests.post("https://example.test/jobs", json={"role": "Engineer"})`,
  });

  assert.equal(explanation.summary, "Run code that exchanges data with an external service.");
  assert.match(explanation.behavior, /send or change data/);
});

test("calls out file changes and subprocesses", () => {
  const explanation = explainApproval({
    tool: "execute_code",
    description: "The code runner requires approval.",
    command: `from pathlib import Path
import subprocess
Path("jobs.json").write_text("[]")
subprocess.run(["open", "jobs.json"])`,
  });

  assert.equal(explanation.summary, "Run a local script that may update files.");
  assert.match(explanation.behavior, /change local files and start other commands/);
});

test("uses a cautious fallback when behavior is unclear", () => {
  const explanation = explainApproval({
    description: "Sensitive local action.",
    command: "custom_tool --opaque-action",
  });

  assert.equal(explanation.summary, "Run a local action to continue this task.");
  assert.equal(explanation.tool, "Local tool");
  assert.match(explanation.reason, /could not classify every effect confidently/);
});

test("explains persistent identity changes in user-facing language", () => {
  const explanation = explainApproval({
    tool: "career_identity_update",
    description: "Allow Pilot to update its identity?",
    command: '{"name":"Zey","soul":"Warm and direct."}',
  });

  assert.equal(explanation.summary, "Permanently update your agent’s identity.");
  assert.equal(explanation.tool, "Identity & SOUL");
  assert.match(explanation.behavior, /across every session/);
  assert.match(explanation.reason, /always asks/);
});
