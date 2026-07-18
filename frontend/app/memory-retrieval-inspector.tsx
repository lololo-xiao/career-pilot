"use client";

import {
  useCallback,
  useEffect,
  useReducer,
  useRef,
  type KeyboardEvent,
  type MouseEvent,
} from "react";

import {
  INITIAL_MEMORY_RETRIEVAL_STATE,
  MEMORY_RETRIEVAL_LABELS,
  MEMORY_RETRIEVAL_PAGE_SIZE,
  MEMORY_RETRIEVAL_RELEVANCE_EXPLANATION,
  memoryRetrievalReducer,
  parseMemoryRetrievalHistory,
  retrievalRelevanceLabel,
  type MemoryRetrievalResolution,
  type MemoryRetrievalSource,
} from "./memory-retrieval";


function formatTimestamp(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function sourceTypeLabel(source: MemoryRetrievalSource) {
  return source.kind === "saved-memory" ? "Saved memory" : "Application outcome";
}

function sourceCitation(source: MemoryRetrievalSource) {
  if (source.kind === "saved-memory") {
    return source.name
      ? `Memory “${source.name}” · version ${source.version}`
      : `Unnamed saved memory · version ${source.version}`;
  }
  return `Recorded status event · ${formatTimestamp(source.recordedAt)}`;
}

function retrievalErrorMessage(status?: number) {
  if (status === 401) return "Your local workspace session has expired.";
  if (status === 404) return "This conversation is no longer available.";
  return "Recent memory retrievals could not be loaded.";
}

function SourceCard({ source }: { source: MemoryRetrievalSource }) {
  const relevanceLabel = retrievalRelevanceLabel(source.relevanceScore);
  return (
    <li className="pilot-retrieval-source">
      <div className="pilot-retrieval-source-heading">
        <div>
          <span>{sourceTypeLabel(source)}</span>
          <cite>{sourceCitation(source)}</cite>
        </div>
        <strong aria-label={relevanceLabel}>{relevanceLabel}</strong>
      </div>
      {source.truncated ? (
        <p className="pilot-retrieval-truncated">
          Shortened source record · the server marked this citation or source summary as truncated.
        </p>
      ) : null}
      <div className="pilot-retrieval-terms">
        <span>Matched terms</span>
        {source.matchedTerms.length ? (
          <ul aria-label="Matched terms">
            {source.matchedTerms.map((term, index) => (
              <li key={`${term}-${index}`}>{term || "(empty term)"}</li>
            ))}
          </ul>
        ) : <p>No matched terms were recorded.</p>}
      </div>
      <div className="pilot-retrieval-why">
        <span>Why Pilot retrieved it</span>
        <p>{source.whyRetrieved}</p>
      </div>
    </li>
  );
}

function ResolutionCard({
  resolution,
  index,
}: {
  resolution: MemoryRetrievalResolution;
  index: number;
}) {
  const sourceLabel = resolution.includedItemCount === 1 ? "1 source" : `${resolution.includedItemCount} sources`;
  return (
    <li className="pilot-retrieval-resolution">
      <details open={index === 0}>
        <summary>
          <span>
            <time dateTime={resolution.createdAt}>{formatTimestamp(resolution.createdAt)}</time>
            <small>{sourceLabel} included</small>
          </span>
          <span aria-hidden="true">⌄</span>
        </summary>
        <div className="pilot-retrieval-resolution-body">
          <dl>
            <div>
              <dt>Retrieval method</dt>
              <dd>{resolution.algorithmLabel}</dd>
            </div>
            <div>
              <dt>Included items</dt>
              <dd>{resolution.includedItemCount}</dd>
            </div>
          </dl>
          {resolution.sources.length ? (
            <ul className="pilot-retrieval-sources" aria-label={`Sources considered ${formatTimestamp(resolution.createdAt)}`}>
              {resolution.sources.map((source, sourceIndex) => (
                <SourceCard
                  key={`${source.kind}-${source.relevanceScore}-${sourceIndex}`}
                  source={source}
                />
              ))}
            </ul>
          ) : (
            <p className="pilot-retrieval-no-sources">
              Pilot recorded the check, but no memory or outcome records were included.
            </p>
          )}
        </div>
      </details>
    </li>
  );
}

export function MemoryRetrievalInspector({
  apiBaseUrl,
  sessionId,
  open,
  refreshKey,
  onClose,
}: {
  apiBaseUrl: string;
  sessionId: string | null;
  open: boolean;
  refreshKey: number;
  onClose: () => void;
}) {
  const [state, dispatch] = useReducer(
    memoryRetrievalReducer,
    INITIAL_MEMORY_RETRIEVAL_STATE,
  );
  const requestIdRef = useRef(0);
  const requestControllerRef = useRef<AbortController | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  const loadPage = useCallback(async (offset: number) => {
    if (!sessionId) return;
    requestControllerRef.current?.abort();
    const controller = new AbortController();
    requestControllerRef.current = controller;
    const requestId = ++requestIdRef.current;
    dispatch({ type: "load-started", sessionId, requestId, offset });

    try {
      const search = new URLSearchParams({
        limit: String(MEMORY_RETRIEVAL_PAGE_SIZE),
        offset: String(offset),
      });
      const response = await fetch(
        `${apiBaseUrl}/companion/sessions/${encodeURIComponent(sessionId)}/memory-retrievals?${search}`,
        {
          credentials: "include",
          cache: "no-store",
          signal: controller.signal,
        },
      );
      if (!response.ok) throw new Error(retrievalErrorMessage(response.status));
      const payload: unknown = await response.json().catch(() => {
        throw new Error("CareerPilot returned an unexpected retrieval history response.");
      });
      const history = parseMemoryRetrievalHistory(payload, {
        sessionId,
        limit: MEMORY_RETRIEVAL_PAGE_SIZE,
        offset,
      });
      dispatch({ type: "load-succeeded", sessionId, requestId, history });
    } catch (caughtError) {
      if (caughtError instanceof DOMException && caughtError.name === "AbortError") return;
      dispatch({
        type: "load-failed",
        sessionId,
        requestId,
        offset,
        message: caughtError instanceof Error
          ? caughtError.message
          : retrievalErrorMessage(),
      });
    }
  }, [apiBaseUrl, sessionId]);

  useEffect(() => {
    dispatch({ type: "session-changed", sessionId });
    if (!open || !sessionId) {
      requestControllerRef.current?.abort();
      return;
    }
    void loadPage(0);
    return () => requestControllerRef.current?.abort();
  }, [loadPage, open, refreshKey, sessionId]);

  useEffect(() => {
    if (!open) return;
    previousFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const frame = window.requestAnimationFrame(() => closeButtonRef.current?.focus());
    return () => {
      window.cancelAnimationFrame(frame);
      previousFocusRef.current?.focus();
    };
  }, [open]);

  if (!open) return null;

  const stateMatchesSession = state.sessionId === sessionId;
  const status = stateMatchesSession ? state.status : "loading";
  const items = stateMatchesSession ? state.items : [];
  const error = stateMatchesSession ? state.error : null;

  function closeOnBackdrop(event: MouseEvent<HTMLDivElement>) {
    if (event.target === event.currentTarget) onClose();
  }

  function handleDialogKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = Array.from(
      event.currentTarget.querySelectorAll<HTMLElement>(
        "button:not([disabled]), summary, a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])",
      ),
    );
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <div className="pilot-retrieval-backdrop" onMouseDown={closeOnBackdrop} role="presentation">
      <section
        aria-busy={status === "loading" || state.isLoadingMore}
        aria-describedby="pilot-retrieval-description pilot-retrieval-boundary"
        aria-labelledby="pilot-retrieval-title"
        aria-modal="true"
        className="pilot-retrieval-drawer"
        id="pilot-retrieval-inspector"
        onKeyDown={handleDialogKeyDown}
        role="dialog"
      >
        <header className="pilot-retrieval-header">
          <div>
            <span>{MEMORY_RETRIEVAL_LABELS.dialogSubtitle}</span>
            <h2 id="pilot-retrieval-title">{MEMORY_RETRIEVAL_LABELS.dialogTitle}</h2>
          </div>
          <button
            aria-label={MEMORY_RETRIEVAL_LABELS.closeButton}
            onClick={onClose}
            ref={closeButtonRef}
            type="button"
          >×</button>
          <p id="pilot-retrieval-description">
            Conversation-level history of recent retrieval checks. It does not show which
            sources shaped any particular answer.
          </p>
        </header>

        <div className="pilot-retrieval-body">
          <div className="pilot-retrieval-boundary" id="pilot-retrieval-boundary">
            <strong>Context, not career evidence</strong>
            <p>
              Saved memories and application outcome records are historical context. They
              are not verified proof of your skills or experience.
            </p>
          </div>
          <p className="pilot-retrieval-score-note">
            <strong>About retrieval relevance:</strong>{" "}
            {MEMORY_RETRIEVAL_RELEVANCE_EXPLANATION}
          </p>

          {status === "loading" ? (
            <div className="pilot-retrieval-loading" role="status" aria-live="polite">
              <span aria-hidden="true" />
              <strong>Checking recent retrieval history…</strong>
              <p>Only privacy-safe citations and resolution reasons will appear here.</p>
            </div>
          ) : status === "error" ? (
            <div className="pilot-retrieval-state is-error" role="alert">
              <strong>Retrieval history is unavailable.</strong>
              <p>{error}</p>
              <button
                aria-label={MEMORY_RETRIEVAL_LABELS.retryButton}
                onClick={() => void loadPage(0)}
                type="button"
              >Try again</button>
            </div>
          ) : items.length === 0 ? (
            <div className="pilot-retrieval-state">
              <strong>No retrieval checks yet.</strong>
              <p>
                When Pilot checks saved memory or recorded outcomes for this conversation,
                a privacy-safe resolution will appear here.
              </p>
            </div>
          ) : (
            <>
              <ul className="pilot-retrieval-list" aria-label="Recent retrieval resolutions">
                {items.map((resolution, index) => (
                  <ResolutionCard
                    index={index}
                    key={`${resolution.createdAt}-${index}`}
                    resolution={resolution}
                  />
                ))}
              </ul>
              {state.loadMoreError ? (
                <p className="pilot-retrieval-more-error" role="alert">
                  {state.loadMoreError} The retrievals already shown are still current.
                </p>
              ) : null}
              {state.hasMore ? (
                <button
                  aria-label={MEMORY_RETRIEVAL_LABELS.loadMoreButton}
                  className="pilot-retrieval-more"
                  disabled={state.isLoadingMore}
                  onClick={() => void loadPage(state.nextOffset)}
                  type="button"
                >
                  {state.isLoadingMore ? "Loading more…" : "Load more"}
                  <span aria-hidden="true">↓</span>
                </button>
              ) : (
                <p className="pilot-retrieval-end">You’ve reached the end of the available history.</p>
              )}
            </>
          )}
        </div>
      </section>
    </div>
  );
}
