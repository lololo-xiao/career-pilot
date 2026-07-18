import assert from "node:assert/strict";
import test from "node:test";

import {
  INITIAL_MEMORY_RETRIEVAL_STATE,
  MEMORY_RETRIEVAL_LABELS,
  MEMORY_RETRIEVAL_RELEVANCE_EXPLANATION,
  MemoryRetrievalResponseError,
  memoryRetrievalReducer,
  parseMemoryRetrievalHistory,
  retrievalAlgorithmLabel,
  retrievalRelevanceLabel,
  type MemoryRetrievalHistory,
} from "../app/memory-retrieval.ts";


const SESSION_A = "00000000-0000-0000-0000-000000000001";
const SESSION_B = "00000000-0000-0000-0000-000000000002";
const HASH_A = "a".repeat(64);
const HASH_B = "b".repeat(64);

function rawHistory(overrides: Record<string, unknown> = {}) {
  return {
    session_id: SESSION_A,
    items: [
      {
        audit_id: "opaque-audit-id",
        created_at: "2026-07-18T09:30:00Z",
        schema_version: "retrieved-memory-v2",
        query_sha256: HASH_A,
        retrieval_algorithm: "weighted-token-overlap-v1",
        content_sha256: HASH_B,
        included_item_count: 2,
        token_upper_bound: 482,
        results: [
          {
            source_type: "memory_revision",
            citation: {
              source_type: "memory_revision",
              revision_id: "private-revision-id",
              name: "interview-preference",
              version: 3,
              source_session: "private-source-session-id",
            },
            relevance_score: 6,
            matched_terms: ["interview", "python"],
            why_retrieved: "Deterministic token overlap matched [interview, python] in memory content; relevance score 6.",
            truncated: false,
          },
          {
            source_type: "application_outcome",
            citation: {
              source_type: "application_status_event",
              event_id: "private-event-id",
              application_id: "private-application-id",
              job_id: "private-job-id",
              recorded_at: "2026-07-16T14:10:00Z",
            },
            relevance_score: 3,
            matched_terms: ["interview"],
            why_retrieved: "Deterministic token overlap matched [interview] in outcome status; relevance score 3.",
            truncated: true,
          },
        ],
      },
    ],
    limit: 5,
    offset: 0,
    has_more: false,
    ...overrides,
  };
}

function parseRaw(value: unknown = rawHistory()) {
  return parseMemoryRetrievalHistory(value, {
    sessionId: SESSION_A,
    limit: 5,
    offset: 0,
  });
}

test("parses the strict API response into a privacy-safe display model", () => {
  const history = parseRaw();

  assert.equal(history.items[0].algorithmLabel, "Deterministic weighted term matching");
  assert.deepEqual(history.items[0].sources, [
    {
      kind: "saved-memory",
      name: "interview-preference",
      version: 3,
      relevanceScore: 6,
      matchedTerms: ["interview", "python"],
      whyRetrieved: "Deterministic token overlap matched [interview, python] in memory content; relevance score 6.",
      truncated: false,
    },
    {
      kind: "application-outcome",
      recordedAt: "2026-07-16T14:10:00Z",
      relevanceScore: 3,
      matchedTerms: ["interview"],
      whyRetrieved: "Deterministic token overlap matched [interview] in outcome status; relevance score 3.",
      truncated: true,
    },
  ]);

  const displayed = JSON.stringify(history);
  for (const privateValue of [
    HASH_A,
    HASH_B,
    "opaque-audit-id",
    "private-revision-id",
    "private-source-session-id",
    "private-event-id",
    "private-application-id",
    "private-job-id",
  ]) {
    assert.doesNotMatch(displayed, new RegExp(privateValue));
  }
  assert.doesNotMatch(displayed, /token_upper_bound|schema_version/);
  assert.match(displayed, /"truncated":true/);
});

test("fails safely instead of accepting query text, content, or unknown response fields", () => {
  const value = rawHistory({ query_text: "show me the private query" });

  assert.throws(
    () => parseRaw(value),
    (error: unknown) => {
      assert.ok(error instanceof MemoryRetrievalResponseError);
      assert.doesNotMatch(error.message, /private query/);
      return true;
    },
  );
});

test("rejects malformed counts, citations, pagination, and cross-session responses", () => {
  const wrongCount = rawHistory();
  (wrongCount.items[0] as Record<string, unknown>).included_item_count = 1;
  assert.throws(() => parseRaw(wrongCount), MemoryRetrievalResponseError);

  const wrongSession = rawHistory({ session_id: SESSION_B });
  assert.throws(() => parseRaw(wrongSession), MemoryRetrievalResponseError);

  const wrongOffset = rawHistory({ offset: 5 });
  assert.throws(() => parseRaw(wrongOffset), MemoryRetrievalResponseError);

  const badCitation = rawHistory();
  const item = badCitation.items[0] as { results: Array<{ citation: Record<string, unknown> }> };
  item.results[0].citation.source_session = 42;
  assert.throws(() => parseRaw(badCitation), MemoryRetrievalResponseError);
});

test("accepts empty legacy opaque IDs, preserves truncation, and still discards IDs", () => {
  const legacy = rawHistory();
  const item = legacy.items[0] as { results: Array<{ citation: Record<string, unknown> }> };
  item.results[0].citation.revision_id = "";
  item.results[0].citation.source_session = "";

  const history = parseRaw(legacy);

  assert.equal(history.items[0].sources[0].truncated, false);
  assert.equal(history.items[0].sources[1].truncated, true);
  assert.doesNotMatch(JSON.stringify(history), /source_session|revision_id/);
});

test("accepts an honest empty retrieval history", () => {
  const history = parseRaw(rawHistory({ items: [] }));

  assert.deepEqual(history.items, []);
  assert.equal(history.hasMore, false);
});

test("uses human language for current, legacy, and unknown server algorithms", () => {
  assert.equal(
    retrievalAlgorithmLabel("weighted-token-overlap-v1"),
    "Deterministic weighted term matching",
  );
  assert.equal(
    retrievalAlgorithmLabel("legacy-active-memory"),
    "Deterministic active-memory matching (legacy)",
  );
  assert.equal(
    retrievalAlgorithmLabel("future-method-with-internal-details"),
    "Server-recorded retrieval method",
  );
});

test("labels retrieval relevance without introducing an ambiguous bare score", () => {
  assert.equal(retrievalRelevanceLabel(6), "Retrieval relevance 6");
  assert.match(MEMORY_RETRIEVAL_RELEVANCE_EXPLANATION, /deterministic term-overlap weight/i);
  assert.match(MEMORY_RETRIEVAL_RELEVANCE_EXPLANATION, /not the 0–10 job fit/i);
  assert.match(MEMORY_RETRIEVAL_RELEVANCE_EXPLANATION, /0–100 queue priority/i);
});

test("represents initial errors and retry success without retaining malformed data", () => {
  let state = memoryRetrievalReducer(INITIAL_MEMORY_RETRIEVAL_STATE, {
    type: "load-started",
    sessionId: SESSION_A,
    requestId: 1,
    offset: 0,
  });
  state = memoryRetrievalReducer(state, {
    type: "load-failed",
    sessionId: SESSION_A,
    requestId: 1,
    offset: 0,
    message: "Recent memory retrievals could not be loaded.",
  });
  assert.equal(state.status, "error");
  assert.deepEqual(state.items, []);

  state = memoryRetrievalReducer(state, {
    type: "load-started",
    sessionId: SESSION_A,
    requestId: 2,
    offset: 0,
  });
  state = memoryRetrievalReducer(state, {
    type: "load-succeeded",
    sessionId: SESSION_A,
    requestId: 2,
    history: parseRaw(rawHistory({ items: [] })),
  });
  assert.equal(state.status, "ready");
  assert.equal(state.error, null);
  assert.deepEqual(state.items, []);
});

test("appends bounded load-more pages and keeps existing items on a page error", () => {
  const firstPage = parseRaw(rawHistory({ has_more: true }));
  let state = memoryRetrievalReducer(INITIAL_MEMORY_RETRIEVAL_STATE, {
    type: "load-started",
    sessionId: SESSION_A,
    requestId: 1,
    offset: 0,
  });
  state = memoryRetrievalReducer(state, {
    type: "load-succeeded",
    sessionId: SESSION_A,
    requestId: 1,
    history: firstPage,
  });
  assert.equal(state.hasMore, true);
  assert.equal(state.nextOffset, 1);

  state = memoryRetrievalReducer(state, {
    type: "load-started",
    sessionId: SESSION_A,
    requestId: 2,
    offset: 1,
  });
  state = memoryRetrievalReducer(state, {
    type: "load-failed",
    sessionId: SESSION_A,
    requestId: 2,
    offset: 1,
    message: "Recent memory retrievals could not be loaded.",
  });
  assert.equal(state.items.length, 1);
  assert.match(state.loadMoreError ?? "", /could not be loaded/);

  const secondPage: MemoryRetrievalHistory = {
    ...firstPage,
    offset: 1,
    hasMore: false,
  };
  state = memoryRetrievalReducer(state, {
    type: "load-started",
    sessionId: SESSION_A,
    requestId: 3,
    offset: 1,
  });
  state = memoryRetrievalReducer(state, {
    type: "load-succeeded",
    sessionId: SESSION_A,
    requestId: 3,
    history: secondPage,
  });
  assert.equal(state.items.length, 2);
  assert.equal(state.hasMore, false);
  assert.equal(state.nextOffset, 2);
});

test("stops offering pagination before the API offset ceiling would be exceeded", () => {
  const page = parseRaw();
  const boundedPage: MemoryRetrievalHistory = {
    ...page,
    offset: 100,
    hasMore: true,
  };
  let state = memoryRetrievalReducer(
    { ...INITIAL_MEMORY_RETRIEVAL_STATE, sessionId: SESSION_A, status: "ready" },
    {
      type: "load-started",
      sessionId: SESSION_A,
      requestId: 4,
      offset: 100,
    },
  );
  state = memoryRetrievalReducer(state, {
    type: "load-succeeded",
    sessionId: SESSION_A,
    requestId: 4,
    history: boundedPage,
  });

  assert.equal(state.nextOffset, 101);
  assert.equal(state.hasMore, false);
});

test("ignores late responses after the active conversation changes", () => {
  let state = memoryRetrievalReducer(INITIAL_MEMORY_RETRIEVAL_STATE, {
    type: "load-started",
    sessionId: SESSION_A,
    requestId: 1,
    offset: 0,
  });
  state = memoryRetrievalReducer(state, {
    type: "session-changed",
    sessionId: SESSION_B,
  });
  state = memoryRetrievalReducer(state, {
    type: "load-started",
    sessionId: SESSION_B,
    requestId: 2,
    offset: 0,
  });

  state = memoryRetrievalReducer(state, {
    type: "load-succeeded",
    sessionId: SESSION_A,
    requestId: 1,
    history: parseRaw(),
  });
  assert.equal(state.sessionId, SESSION_B);
  assert.equal(state.status, "loading");
  assert.deepEqual(state.items, []);
});

test("publishes explicit accessible labels for every inspector control", () => {
  assert.deepEqual(MEMORY_RETRIEVAL_LABELS, {
    openButton: "Open recent memory retrievals",
    closeButton: "Close recent memory retrievals",
    dialogTitle: "Recent memory retrievals",
    dialogSubtitle: "What Pilot considered recently",
    retryButton: "Retry loading recent memory retrievals",
    loadMoreButton: "Load more memory retrievals",
  });
});
