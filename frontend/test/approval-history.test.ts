import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  ApprovalHistoryRequestCoordinator,
  approvalScopeLabel,
  approvalStateCopy,
  approvalStatePresentation,
  effectiveApprovalState,
  effectiveApprovalUsability,
  mergeApprovalHistory,
  parseApprovalHistoryPage,
  parseSessionAccountId,
  type ApprovalHistoryItem,
} from "../app/workspace/approval-history.ts";


function cursor(version = 1): string {
  return Buffer.from(JSON.stringify({
    i: "approval_1",
    t: "2026-07-19T12:00:00Z",
    v: version,
  })).toString("base64url");
}


function response(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema_version: "approval-history-v1",
    as_of: "2026-07-19T12:00:00Z",
    pending_count: 1,
    items: [record()],
    next_cursor: cursor(),
    ...overrides,
  };
}


function record(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "approval_1",
    state: "approved",
    usable: true,
    requested_at: "2026-07-19T11:00:00+00:00",
    last_transition_at: "2026-07-19T11:05:00Z",
    expires_at: "2026-07-19T12:30:00Z",
    request_summary: "Fill the Example form",
    authorization: {
      title: "Fill one application form",
      effect: "Fill the exact non-submit controls.",
      not_authorized: ["Submitting the form", "A second use"],
      context: [{ label: "Target", value: "jobs.example.test" }],
      binding: {
        action_type: "application.form_fill",
        algorithm: "sha256-canonical-json-v1",
        payload_sha256: "a".repeat(64),
        maximum_uses: 1,
        remaining_uses: 1,
      },
    },
    ...overrides,
  };
}


test("parses the bounded versioned history contract without coercion", () => {
  const page = parseApprovalHistoryPage(response());

  assert.equal(page.pendingCount, 1);
  assert.equal(page.items[0].authorization.binding.actionType, "application.form_fill");
  assert.equal(page.items[0].authorization.binding.payloadSha256, "a".repeat(64));
  assert.equal(page.nextCursor, cursor());
});


test("requires timestamps with an explicit timezone", () => {
  for (const invalid of [
    "2026-07-19",
    "2026-07-19T12:00:00",
    "July 19, 2026 12:00 UTC",
    "not-a-date",
  ]) {
    assert.throws(
      () => parseApprovalHistoryPage(response({ as_of: invalid })),
      /invalid date/,
    );
  }
  assert.doesNotThrow(() => parseApprovalHistoryPage(response({
    as_of: "2026-07-19T14:00:00+02:00",
  })));
});


test("requires a strict base64url versioned cursor", () => {
  for (const invalid of [
    "plain-text",
    "has+padding=",
    Buffer.from(JSON.stringify({ i: "approval_1", t: "2026-07-19T12:00:00Z", v: 2 })).toString("base64url"),
    Buffer.from(JSON.stringify({ i: "approval_1", t: "2026-07-19T12:00:00", v: 1 })).toString("base64url"),
    Buffer.from(JSON.stringify({ i: "approval_1", t: "2026-07-19T12:00:00Z", v: 1, extra: true })).toString("base64url"),
    "a".repeat(513),
  ]) {
    assert.throws(
      () => parseApprovalHistoryPage(response({ next_cursor: invalid })),
      /invalid cursor/,
    );
  }
});


test("rejects inconsistent state, usability, and remaining-use claims", () => {
  const inconsistent = [
    record({ state: "pending", usable: true }),
    record({ state: "consumed", usable: true }),
    record({
      usable: false,
      authorization: {
        ...(record().authorization as Record<string, unknown>),
        binding: {
          ...((record().authorization as Record<string, unknown>).binding as Record<string, unknown>),
          remaining_uses: 1,
        },
      },
    }),
    record({
      authorization: {
        ...(record().authorization as Record<string, unknown>),
        binding: {
          ...((record().authorization as Record<string, unknown>).binding as Record<string, unknown>),
          payload_sha256: null,
        },
      },
    }),
  ];
  for (const item of inconsistent) {
    assert.throws(
      () => parseApprovalHistoryPage(response({ items: [item] })),
      /inconsistent request binding/,
    );
  }
});


test("rejects malformed hashes, states, oversized pages, and summaries", () => {
  assert.throws(
    () => parseApprovalHistoryPage(response({ items: [record({ state: "allowed" })] })),
    /invalid state/,
  );
  const malformedAuthorization = record().authorization as Record<string, unknown>;
  assert.throws(
    () => parseApprovalHistoryPage(response({
      items: [record({
        authorization: {
          ...malformedAuthorization,
          binding: {
            ...(malformedAuthorization.binding as Record<string, unknown>),
            payload_sha256: "not-a-hash",
          },
        },
      })],
    })),
    /invalid request fingerprint/,
  );
  assert.throws(
    () => parseApprovalHistoryPage(response({ items: Array.from({ length: 51 }, record) })),
    /too many records/,
  );
  assert.throws(
    () => parseApprovalHistoryPage(response({ items: [record({ request_summary: "x".repeat(281) })] })),
    /invalid request summary/,
  );
});


test("expires approved and pending records locally without changing terminal states", () => {
  const parsed = parseApprovalHistoryPage(response({ next_cursor: null }));
  const approved = parsed.items[0];
  assert.equal(effectiveApprovalState(approved, Date.parse("2026-07-19T12:29:00Z")), "approved");
  assert.equal(effectiveApprovalState(approved, Date.parse("2026-07-19T12:30:00Z")), "expired");
  assert.equal(effectiveApprovalUsability(approved, Date.parse("2026-07-19T12:30:00Z")), false);
  const expiredPresentation = approvalStatePresentation(
    effectiveApprovalState(approved, Date.parse("2026-07-19T12:30:00Z")),
    effectiveApprovalUsability(approved, Date.parse("2026-07-19T12:30:00Z")),
  );
  assert.equal(expiredPresentation.label, "Expired");
  assert.match(expiredPresentation.copy, /authorizes no action/);
  assert.equal(approvalScopeLabel(false), "Recorded scope");
  assert.equal(
    effectiveApprovalState({ ...approved, state: "consumed" }, Date.parse("2027-01-01T00:00:00Z")),
    "consumed",
  );
  assert.match(approvalStateCopy("consumed"), /cannot be reused/);
  assert.match(approvalStateCopy("denied"), /authorizes no action/);
});


test("state presentation distinguishes usable approvals from reserved records", () => {
  const cases = [
    ["pending", false, "Waiting", 0, /cannot be used/],
    ["approved", true, "Allowed once", 1, /Available for one exact matching action/],
    ["approved", false, "Recorded only", 0, /no enabled CareerPilot consumer/],
    ["consumed", false, "Used", 0, /cannot be reused/],
    ["denied", false, "Denied", 0, /authorizes no action/],
    ["expired", false, "Expired", 0, /authorizes no action/],
  ] as const;

  for (const [state, usable, label, remainingUses, copy] of cases) {
    const presentation = approvalStatePresentation(state, usable);
    assert.equal(presentation.label, label);
    assert.equal(presentation.remainingUses, remainingUses);
    assert.match(presentation.copy, copy);
  }

  const authorization = record().authorization as Record<string, unknown>;
  const reserved = parseApprovalHistoryPage(response({
    items: [record({
      usable: false,
      authorization: {
        ...authorization,
        title: "Reserved email-draft approval",
        binding: {
          ...(authorization.binding as Record<string, unknown>),
          action_type: "email.draft",
          remaining_uses: 0,
        },
      },
    })],
    next_cursor: null,
  })).items[0];
  assert.deepEqual(
    approvalStatePresentation(
      effectiveApprovalState(reserved, Date.parse("2026-07-19T12:00:00Z")),
      reserved.usable,
    ),
    {
      label: "Recorded only",
      copy: "Approved as a record, but no enabled CareerPilot consumer can use it.",
      remainingUses: 0,
    },
  );
});


test("coordinator aborts stale account generations and merge deduplicates IDs", () => {
  const coordinator = new ApprovalHistoryRequestCoordinator();
  const first = coordinator.start("account-a");
  const second = coordinator.start("account-a");
  assert.equal(first.signal.aborted, true);
  assert.equal(first.isCurrent(), false);
  assert.equal(second.isCurrent(), true);

  const third = coordinator.start("account-b");
  assert.equal(second.signal.aborted, true);
  assert.equal(third.isCurrent(), true);
  coordinator.invalidate();
  assert.equal(third.isCurrent(), false);

  const parsed = parseApprovalHistoryPage(response({ next_cursor: null })).items[0];
  const additional: ApprovalHistoryItem = { ...parsed, id: "approval_2" };
  assert.deepEqual(
    mergeApprovalHistory([parsed], [parsed, additional], true).map((item) => item.id),
    ["approval_1", "approval_2"],
  );
});


test("local session parsing binds history to the exact account", () => {
  assert.equal(parseSessionAccountId({ authenticated: true, user: { id: "account-a" } }), "account-a");
  assert.equal(parseSessionAccountId({ authenticated: false, user: null }), null);
  assert.throws(() => parseSessionAccountId({ authenticated: true, user: { id: 3 } }), /invalid local account/);
});


test("panel is read-only, renders React text, and exposes the collapsed binding summary", () => {
  const panel = readFileSync(
    new URL("../app/workspace/approval-history-panel.tsx", import.meta.url),
    "utf8",
  );
  const workspace = readFileSync(
    new URL("../app/workspace/page.tsx", import.meta.url),
    "utf8",
  );

  assert.doesNotMatch(panel, /dangerouslySetInnerHTML|\.innerHTML|DOMParser|eval\s*\(/);
  assert.doesNotMatch(panel, /method:\s*["'](?:POST|PUT|PATCH|DELETE)/);
  assert.doesNotMatch(panel, /\/decision|\/browser\/fill|\/application\.form_fill/);
  assert.match(panel, /Exact match/);
  assert.match(panel, /abbreviatedDigest/);
  assert.match(panel, /Exact match and timestamps/);
  assert.match(panel, /payloadSha256/);
  assert.match(panel, /aria-live="polite"/);
  assert.match(panel, /role="alert"/);
  assert.match(panel, /const \[loading, setLoading\] = useState\(true\)/);
  assert.equal(workspace.includes('apiRequest<Approval[]>("/api/v1/approvals")'), false);
  assert.match(workspace, /\/api\/v1\/approvals\/history\?limit=1/);
});
