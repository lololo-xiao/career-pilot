import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  jobKey,
  parseJobWriteResult,
  parseLocalSessionResponse,
  parsePublicDiscoveryResponse,
  PreviewRequestCoordinator,
  previewBindingMatches,
  saveJobSnapshotSequentially,
  snapshotSelectedJobs,
  type DiscoveredJob,
} from "../app/discover/discovery.ts";


function job(id: string): DiscoveredJob {
  return {
    title: `Role ${id}`,
    company: "Example Labs",
    locations: ["Remote"],
    description: `Description ${id}`,
    requirements: [],
    preferred: [],
    seniority: "unknown",
    employment_type: "Full-time",
    workplace_type: "unknown",
    company_size: "unknown",
    posted_date: null,
    deadline: null,
    source_url: `https://boards.greenhouse.io/example/jobs/${id}`,
    source_type: "greenhouse",
  };
}


test("new previews abort and suppress stale or cancelled generations", () => {
  const coordinator = new PreviewRequestCoordinator();
  const first = coordinator.start();
  const second = coordinator.start();

  assert.equal(first.signal.aborted, true);
  assert.equal(first.isCurrent(), false);
  assert.equal(second.isCurrent(), true);

  coordinator.invalidate();
  assert.equal(second.signal.aborted, true);
  assert.equal(second.isCurrent(), false);
});

test("preview binding fails closed across account, provider, identifier, and generation", () => {
  const binding = {
    accountId: "account-a",
    provider: "greenhouse" as const,
    companyIdentifier: "example",
    generation: 4,
  };

  assert.equal(previewBindingMatches(binding, "account-a", "greenhouse", " example ", 4), true);
  assert.equal(previewBindingMatches(binding, "account-b", "greenhouse", "example", 4), false);
  assert.equal(previewBindingMatches(binding, "account-a", "lever", "example", 4), false);
  assert.equal(previewBindingMatches(binding, "account-a", "greenhouse", "other", 4), false);
  assert.equal(previewBindingMatches(binding, "account-a", "greenhouse", "example", 5), false);
});

test("selection starts empty and the checked save snapshot is isolated from later edits", () => {
  const jobs = [job("one"), job("two")];
  const initiallySelected = new Set<string>();
  assert.deepEqual(snapshotSelectedJobs(jobs, initiallySelected), []);

  const selected = new Set([jobKey(jobs[1])]);
  const snapshot = snapshotSelectedJobs(jobs, selected);
  jobs[1].description = "Changed after save began";
  jobs[1].locations.push("Elsewhere");

  assert.equal(snapshot.length, 1);
  assert.equal(snapshot[0].description, "Description two");
  assert.deepEqual(snapshot[0].locations, ["Remote"]);
});

test("checked jobs save sequentially and failed saves remain identifiable for retry", async () => {
  const jobs = [job("one"), job("two"), job("three")];
  const calls: string[] = [];
  let inFlight = 0;
  let maxInFlight = 0;

  const result = await saveJobSnapshotSequentially(
    jobs,
    "account-a",
    async () => "account-a",
    async (candidate) => {
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      calls.push(candidate.title);
      inFlight -= 1;
      if (candidate.title === "Role two") throw new Error("Save not confirmed — retry safely");
      return { created: candidate.title === "Role one" };
    },
  );

  assert.deepEqual(calls, ["Role one", "Role two", "Role three"]);
  assert.equal(maxInFlight, 1);
  assert.deepEqual(result.outcomes.map((outcome) => outcome.state), ["saved", "failed", "already"]);
  assert.equal(result.outcomes[2].message, "Already in queue");
  assert.equal(result.outcomes[1].key, jobKey(jobs[1]));
});

test("account changes stop later writes without undoing an already confirmed save", async () => {
  const jobs = [job("one"), job("two")];
  const accountReads = ["account-a", "account-b"];
  const writes: string[] = [];

  const result = await saveJobSnapshotSequentially(
    jobs,
    "account-a",
    async () => accountReads.shift() ?? "account-b",
    async (candidate) => {
      writes.push(candidate.title);
      return { created: true };
    },
  );

  assert.deepEqual(writes, ["Role one"]);
  assert.equal(result.accountChanged, true);
  assert.equal(result.currentAccountId, "account-b");
  assert.equal(result.outcomes[0].state, "saved");
});

test("strict session parsing distinguishes signed-out, disconnected, and connected accounts", () => {
  assert.deepEqual(
    parseLocalSessionResponse({ authenticated: false, user: null }),
    { authenticated: false, user: null },
  );
  const disconnected = parseLocalSessionResponse({
    authenticated: true,
    user: {
      id: "account-a",
      display_name: "Local workspace",
      active_provider: null,
      plan_type: null,
      provider_email: null,
      provider_label: null,
    },
  });
  assert.equal(disconnected.user?.active_provider, null);
  assert.throws(
    () => parseLocalSessionResponse({ authenticated: "yes", user: { id: "account-a" } }),
    /Local session returned an invalid response/,
  );
  assert.throws(
    () => parseLocalSessionResponse({ authenticated: true, user: { id: "account-a" } }),
    /Local session returned an invalid response/,
  );
});

test("strict preview parsing binds activity and enforces counts, bounds, arrays, and safe URLs", () => {
  const candidate = job("one");
  const valid = {
    activity: {
      type: "public_network_read",
      provider: "greenhouse",
      company_identifier: "example",
    },
    discovered: 1,
    returned: 1,
    jobs: [candidate],
    stored: 0,
  };
  assert.equal(
    parsePublicDiscoveryResponse(valid, "greenhouse", " example ", 10).jobs[0].title,
    "Role one",
  );

  for (const malformed of [
    { ...valid, stored: 1 },
    { ...valid, returned: 26, jobs: Array.from({ length: 26 }, (_, index) => job(String(index))) },
    { ...valid, returned: 2 },
    { ...valid, activity: { ...valid.activity, provider: "lever" } },
    { ...valid, activity: { ...valid.activity, company_identifier: "other" } },
    { ...valid, jobs: [{ ...candidate, locations: "Remote" }] },
    { ...valid, jobs: [{ ...candidate, description: "x".repeat(50_001) }] },
    { ...valid, jobs: [{ ...candidate, source_url: "https://user:secret@example.test/job" }] },
    { ...valid, jobs: [{ ...candidate, source_type: "lever" }] },
  ]) {
    assert.throws(
      () => parsePublicDiscoveryResponse(malformed, "greenhouse", "example", 25),
      /Public job preview returned an invalid response/,
    );
  }
});

test("job save parsing never coerces a malformed created value", () => {
  assert.deepEqual(parseJobWriteResult({ id: "job-a", created: false }), { created: false });
  assert.throws(() => parseJobWriteResult({ id: "job-a", created: "false" }), /invalid response/);
  assert.throws(() => parseJobWriteResult({ created: true }), /invalid response/);
});

test("guided page renders provider data as React text and calls only preview and job-write APIs", () => {
  const source = readFileSync(new URL("../app/discover/page.tsx", import.meta.url), "utf8");

  assert.doesNotMatch(source, /dangerouslySetInnerHTML|\.innerHTML|DOMParser|eval\s*\(/);
  assert.match(source, /\{job\.title\}/);
  assert.match(source, /\{job\.description/);
  assert.equal(source.match(/\/api\/v1\/jobs\/discover-public/g)?.length, 1);
  assert.equal(source.match(/`\$\{API_BASE_URL\}\/api\/v1\/jobs`/g)?.length, 1);
  for (const forbidden of [
    "/score",
    "/applications",
    "/approvals",
    "/revisions",
    "/audit",
    "/browser/fill",
    "/mcp",
    "/messages",
  ]) {
    assert.equal(source.includes(forbidden), false, `must not call ${forbidden}`);
  }
});
