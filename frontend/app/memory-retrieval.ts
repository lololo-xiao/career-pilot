export const MEMORY_RETRIEVAL_PAGE_SIZE = 5;
export const MEMORY_RETRIEVAL_MAX_OFFSET = 100;

export const MEMORY_RETRIEVAL_LABELS = {
  openButton: "Open recent memory retrievals",
  closeButton: "Close recent memory retrievals",
  dialogTitle: "Recent memory retrievals",
  dialogSubtitle: "What Pilot considered recently",
  retryButton: "Retry loading recent memory retrievals",
  loadMoreButton: "Load more memory retrievals",
} as const;

export const MEMORY_RETRIEVAL_RELEVANCE_EXPLANATION =
  "This is a deterministic term-overlap weight. It is not the 0–10 job fit or the 0–100 queue priority score.";

export function retrievalRelevanceLabel(score: number) {
  return `Retrieval relevance ${score}`;
}

export interface SavedMemoryRetrievalSource {
  kind: "saved-memory";
  name: string;
  version: number;
  relevanceScore: number;
  matchedTerms: string[];
  whyRetrieved: string;
  truncated: boolean;
}

export interface ApplicationOutcomeRetrievalSource {
  kind: "application-outcome";
  recordedAt: string;
  relevanceScore: number;
  matchedTerms: string[];
  whyRetrieved: string;
  truncated: boolean;
}

export type MemoryRetrievalSource =
  | SavedMemoryRetrievalSource
  | ApplicationOutcomeRetrievalSource;

export interface MemoryRetrievalResolution {
  createdAt: string;
  algorithmLabel: string;
  includedItemCount: number;
  sources: MemoryRetrievalSource[];
}

export interface MemoryRetrievalHistory {
  sessionId: string;
  items: MemoryRetrievalResolution[];
  limit: number;
  offset: number;
  hasMore: boolean;
}

export class MemoryRetrievalResponseError extends Error {
  constructor() {
    super("CareerPilot returned an unexpected retrieval history response.");
    this.name = "MemoryRetrievalResponseError";
  }
}

function invalidResponse(): never {
  throw new MemoryRetrievalResponseError();
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, expected: readonly string[]) {
  const actual = Object.keys(value).sort();
  const required = [...expected].sort();
  return actual.length === required.length
    && actual.every((key, index) => key === required[index]);
}

function readString(value: unknown, minimum: number, maximum: number) {
  if (typeof value !== "string" || value.length < minimum || value.length > maximum) {
    invalidResponse();
  }
  return value;
}

function readInteger(value: unknown, minimum: number, maximum?: number) {
  if (
    typeof value !== "number"
    || !Number.isSafeInteger(value)
    || value < minimum
    || (maximum !== undefined && value > maximum)
  ) {
    invalidResponse();
  }
  return value;
}

function readDate(value: unknown) {
  const date = readString(value, 1, 100);
  if (Number.isNaN(Date.parse(date))) invalidResponse();
  return date;
}

function readHash(value: unknown, nullable = false) {
  if (nullable && value === null) return null;
  const hash = readString(value, 64, 64);
  if (!/^[0-9a-f]{64}$/.test(hash)) invalidResponse();
  return hash;
}

function readOpaqueIdentifier(value: unknown) {
  return readString(value, 0, 240);
}

function readMatchedTerms(value: unknown) {
  if (!Array.isArray(value) || value.length > 32) invalidResponse();
  return value.map((term) => readString(term, 0, 64));
}

export function retrievalAlgorithmLabel(algorithm: string) {
  if (algorithm === "weighted-token-overlap-v1") {
    return "Deterministic weighted term matching";
  }
  if (algorithm === "legacy-active-memory") {
    return "Deterministic active-memory matching (legacy)";
  }
  return "Server-recorded retrieval method";
}

function parseMemoryCitation(value: unknown) {
  if (!isRecord(value) || !hasExactKeys(value, [
    "source_type",
    "revision_id",
    "name",
    "version",
    "source_session",
  ])) {
    invalidResponse();
  }
  if (value.source_type !== "memory_revision") invalidResponse();

  // Validate opaque identifiers because they are part of the strict server contract,
  // then intentionally discard them from the display model.
  readOpaqueIdentifier(value.revision_id);
  readOpaqueIdentifier(value.source_session);
  return {
    name: readString(value.name, 0, 240),
    version: readInteger(value.version, 1),
  };
}

function parseOutcomeCitation(value: unknown) {
  if (!isRecord(value) || !hasExactKeys(value, [
    "source_type",
    "event_id",
    "application_id",
    "job_id",
    "recorded_at",
  ])) {
    invalidResponse();
  }
  if (value.source_type !== "application_status_event") invalidResponse();

  readOpaqueIdentifier(value.event_id);
  readOpaqueIdentifier(value.application_id);
  readOpaqueIdentifier(value.job_id);
  return { recordedAt: readDate(value.recorded_at) };
}

function parseSource(value: unknown): MemoryRetrievalSource {
  if (!isRecord(value) || !hasExactKeys(value, [
    "source_type",
    "citation",
    "relevance_score",
    "matched_terms",
    "why_retrieved",
    "truncated",
  ])) {
    invalidResponse();
  }
  if (typeof value.truncated !== "boolean") invalidResponse();
  const shared = {
    relevanceScore: readInteger(value.relevance_score, 1),
    matchedTerms: readMatchedTerms(value.matched_terms),
    whyRetrieved: readString(value.why_retrieved, 1, 1_000),
    truncated: value.truncated,
  };

  if (value.source_type === "memory_revision") {
    const citation = parseMemoryCitation(value.citation);
    return {
      kind: "saved-memory",
      name: citation.name,
      version: citation.version,
      ...shared,
    };
  }
  if (value.source_type === "application_outcome") {
    const citation = parseOutcomeCitation(value.citation);
    return {
      kind: "application-outcome",
      recordedAt: citation.recordedAt,
      ...shared,
    };
  }
  return invalidResponse();
}

function parseResolution(value: unknown): MemoryRetrievalResolution {
  if (!isRecord(value) || !hasExactKeys(value, [
    "audit_id",
    "created_at",
    "schema_version",
    "query_sha256",
    "retrieval_algorithm",
    "content_sha256",
    "included_item_count",
    "token_upper_bound",
    "results",
  ])) {
    invalidResponse();
  }

  readOpaqueIdentifier(value.audit_id);
  readString(value.schema_version, 1, 50);
  readHash(value.query_sha256, true);
  readHash(value.content_sha256);
  readInteger(value.token_upper_bound, 0, 8_192);
  const algorithm = readString(value.retrieval_algorithm, 1, 100);
  const includedItemCount = readInteger(value.included_item_count, 0, 6);
  if (!Array.isArray(value.results) || value.results.length > 6) invalidResponse();
  const sources = value.results.map(parseSource);
  if (sources.length !== includedItemCount) invalidResponse();

  return {
    createdAt: readDate(value.created_at),
    algorithmLabel: retrievalAlgorithmLabel(algorithm),
    includedItemCount,
    sources,
  };
}

export function parseMemoryRetrievalHistory(
  value: unknown,
  expected: { sessionId: string; limit: number; offset: number },
): MemoryRetrievalHistory {
  if (!isRecord(value) || !hasExactKeys(value, [
    "session_id",
    "items",
    "limit",
    "offset",
    "has_more",
  ])) {
    invalidResponse();
  }

  const sessionId = readOpaqueIdentifier(value.session_id);
  const limit = readInteger(value.limit, 1, 10);
  const offset = readInteger(value.offset, 0, 100);
  if (
    sessionId !== expected.sessionId
    || limit !== expected.limit
    || offset !== expected.offset
    || typeof value.has_more !== "boolean"
    || !Array.isArray(value.items)
    || value.items.length > limit
  ) {
    invalidResponse();
  }
  const items = value.items.map(parseResolution);
  if (value.has_more && items.length === 0) invalidResponse();

  return {
    sessionId,
    items,
    limit,
    offset,
    hasMore: value.has_more,
  };
}

export type MemoryRetrievalStatus = "idle" | "loading" | "ready" | "error";

export interface MemoryRetrievalState {
  sessionId: string | null;
  status: MemoryRetrievalStatus;
  items: MemoryRetrievalResolution[];
  hasMore: boolean;
  nextOffset: number;
  requestId: number;
  isLoadingMore: boolean;
  error: string | null;
  loadMoreError: string | null;
}

export type MemoryRetrievalAction =
  | { type: "session-changed"; sessionId: string | null }
  | { type: "load-started"; sessionId: string; requestId: number; offset: number }
  | { type: "load-succeeded"; sessionId: string; requestId: number; history: MemoryRetrievalHistory }
  | { type: "load-failed"; sessionId: string; requestId: number; offset: number; message: string };

export const INITIAL_MEMORY_RETRIEVAL_STATE: MemoryRetrievalState = {
  sessionId: null,
  status: "idle",
  items: [],
  hasMore: false,
  nextOffset: 0,
  requestId: 0,
  isLoadingMore: false,
  error: null,
  loadMoreError: null,
};

export function memoryRetrievalReducer(
  state: MemoryRetrievalState,
  action: MemoryRetrievalAction,
): MemoryRetrievalState {
  if (action.type === "session-changed") {
    if (state.sessionId === action.sessionId) return state;
    return { ...INITIAL_MEMORY_RETRIEVAL_STATE, sessionId: action.sessionId };
  }

  if (action.type === "load-started") {
    const replacing = action.offset === 0 || state.sessionId !== action.sessionId;
    return {
      ...(replacing ? INITIAL_MEMORY_RETRIEVAL_STATE : state),
      sessionId: action.sessionId,
      status: replacing ? "loading" : "ready",
      requestId: action.requestId,
      isLoadingMore: !replacing,
      error: null,
      loadMoreError: null,
    };
  }

  if (state.sessionId !== action.sessionId || state.requestId !== action.requestId) {
    return state;
  }

  if (action.type === "load-succeeded") {
    const replacing = action.history.offset === 0;
    const nextOffset = action.history.offset + action.history.items.length;
    return {
      ...state,
      status: "ready",
      items: replacing ? action.history.items : [...state.items, ...action.history.items],
      hasMore: action.history.hasMore && nextOffset <= MEMORY_RETRIEVAL_MAX_OFFSET,
      nextOffset,
      isLoadingMore: false,
      error: null,
      loadMoreError: null,
    };
  }

  if (action.type === "load-failed") {
    if (action.offset === 0) {
      return {
        ...state,
        status: "error",
        items: [],
        hasMore: false,
        nextOffset: 0,
        isLoadingMore: false,
        error: action.message,
      };
    }
    return {
      ...state,
      isLoadingMore: false,
      loadMoreError: action.message,
    };
  }

  return state;
}
