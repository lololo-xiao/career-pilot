"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { API_BASE_URL } from "./api";
import {
  ApprovalHistoryRequestCoordinator,
  approvalStatePresentation,
  effectiveApprovalState,
  mergeApprovalHistory,
  parseApprovalHistoryPage,
  parseSessionAccountId,
  type ApprovalHistoryItem,
} from "./approval-history";
import styles from "./approval-history.module.css";

interface ApprovalHistoryPanelProps {
  accountId: string;
  refreshKey: string;
}

async function responseError(response: Response, fallback: string): Promise<string> {
  const payload = (await response.json().catch(() => null)) as { detail?: unknown } | null;
  return typeof payload?.detail === "string" ? payload.detail : fallback;
}

function formatMoment(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function abbreviatedDigest(value: string | null): string {
  return value ? `${value.slice(0, 10)}…${value.slice(-8)}` : "binding unavailable";
}

export function ApprovalHistoryPanel({
  accountId,
  refreshKey,
}: ApprovalHistoryPanelProps) {
  return (
    <ApprovalHistoryPanelBody
      accountId={accountId}
      key={`${accountId}:${refreshKey}`}
    />
  );
}

function ApprovalHistoryPanelBody({ accountId }: { accountId: string }) {
  const coordinator = useRef(new ApprovalHistoryRequestCoordinator());
  const [items, setItems] = useState<ApprovalHistoryItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [asOf, setAsOf] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const load = useCallback(async (cursor: string | null, append: boolean) => {
    const request = coordinator.current.start(accountId);
    if (append) setLoadingOlder(true);
    else setLoading(true);
    setError(null);
    try {
      const query = new URLSearchParams({ limit: "20" });
      if (cursor) query.set("cursor", cursor);
      const historyResponse = await fetch(
        `${API_BASE_URL}/api/v1/approvals/history?${query.toString()}`,
        { credentials: "include", signal: request.signal },
      );
      if (!historyResponse.ok) {
        throw new Error(await responseError(historyResponse, "Approval history could not be loaded."));
      }
      const page = parseApprovalHistoryPage(await historyResponse.json());
      const sessionResponse = await fetch(`${API_BASE_URL}/local/session`, {
        credentials: "include",
        signal: request.signal,
      });
      if (!sessionResponse.ok) {
        throw new Error("The local account changed while approval history was loading.");
      }
      const responseAccountId = parseSessionAccountId(await sessionResponse.json());
      if (!request.isCurrent() || responseAccountId !== accountId) return;
      setItems((current) => mergeApprovalHistory(current, page.items, append));
      setNextCursor(page.nextCursor);
      setAsOf(page.asOf);
      setNow(Date.now());
    } catch (cause) {
      if (request.signal.aborted || !request.isCurrent()) return;
      setError(cause instanceof Error ? cause.message : "Approval history could not be loaded.");
    } finally {
      if (request.isCurrent()) {
        setLoading(false);
        setLoadingOlder(false);
      }
    }
  }, [accountId]);

  useEffect(() => {
    const activeCoordinator = coordinator.current;
    const timer = window.setTimeout(() => void load(null, false), 0);
    return () => {
      window.clearTimeout(timer);
      activeCoordinator.invalidate();
    };
  }, [load]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  return (
    <section className={`workspace-card ${styles.panel}`} aria-labelledby="approval-history-title">
      <div className={styles.heading}>
        <div>
          <h2 id="approval-history-title">Approval records</h2>
          <p>Local, one-use records—not reusable passwords.</p>
        </div>
        <button
          className="workspace-button-quiet"
          disabled={loading || loadingOlder}
          onClick={() => void load(null, false)}
          type="button"
        >
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      <p className={styles.explainer}>
        For enabled actions, CareerPilot accepts an approved record once only before
        expiry, for the named action and exact request whose fingerprint is shown.
      </p>

      <div aria-live="polite" className={styles.status}>
        {loading && !items.length ? "Loading approval records…" : null}
        {asOf && !loading ? `Checked ${formatMoment(asOf)}` : null}
      </div>

      {error ? (
        <div className={styles.error} role="alert">
          <span>{error}</span>
          <button onClick={() => void load(null, false)} type="button">Try again</button>
        </div>
      ) : null}

      <div className={styles.list}>
        {items.map((item) => {
          const state = effectiveApprovalState(item, now);
          const presentation = approvalStatePresentation(state, item.usable);
          const badgeClass = state === "approved" && !item.usable ? "recorded" : state;
          return (
            <article className={styles.record} key={item.id}>
              <header>
                <div>
                  <span className={`${styles.badge} ${styles[badgeClass]}`}>{presentation.label}</span>
                  <h3>{item.authorization.title}</h3>
                </div>
                <strong>{presentation.remainingUses}/1 use remaining</strong>
              </header>
              <p className={styles.stateCopy}>{presentation.copy}</p>
              <p className={styles.bindingSummary}>
                Exact match <code>{item.authorization.binding.actionType}</code>
                {" · "}<code>{abbreviatedDigest(item.authorization.binding.payloadSha256)}</code>
              </p>

              <div className={styles.authorization}>
                <div>
                  <h4>{item.usable ? "Authorizes" : "Recorded scope"}</h4>
                  <p>{item.authorization.effect}</p>
                </div>
                <div>
                  <h4>Does not authorize</h4>
                  <ul>
                    {item.authorization.notAuthorized.map((limit) => <li key={limit}>{limit}</li>)}
                  </ul>
                </div>
              </div>

              {item.authorization.context.length ? (
                <dl className={styles.context}>
                  {item.authorization.context.map((entry) => (
                    <div key={entry.label}>
                      <dt>{entry.label}</dt>
                      <dd>{entry.value}</dd>
                    </div>
                  ))}
                </dl>
              ) : null}

              {item.requestSummary ? (
                <div className={styles.summary}>
                  <strong>Recorded request summary</strong>
                  <p>{item.requestSummary}</p>
                  <small>The fingerprint—not this summary—is what CareerPilot enforces.</small>
                </div>
              ) : null}

              <details className={styles.technical}>
                <summary>Exact match and timestamps</summary>
                <dl>
                  <div><dt>Action type</dt><dd><code>{item.authorization.binding.actionType}</code></dd></div>
                  <div><dt>Request fingerprint</dt><dd><code>{item.authorization.binding.payloadSha256 ?? "Unavailable — this record cannot be used"}</code></dd></div>
                  <div><dt>Requested</dt><dd>{formatMoment(item.requestedAt)}</dd></div>
                  <div><dt>Expires</dt><dd>{formatMoment(item.expiresAt)}</dd></div>
                  {item.lastTransitionAt ? <div><dt>Last changed</dt><dd>{formatMoment(item.lastTransitionAt)}</dd></div> : null}
                </dl>
              </details>
            </article>
          );
        })}
        {!loading && !error && !items.length ? (
          <div className={styles.empty}>
            <strong>No durable action approvals yet.</strong>
            <span>When an external action requests permission, its exact scope will appear here.</span>
          </div>
        ) : null}
      </div>

      {nextCursor ? (
        <button
          className={styles.loadMore}
          disabled={loadingOlder || loading}
          onClick={() => void load(nextCursor, true)}
          type="button"
        >
          {loadingOlder ? "Loading…" : "Load older approvals"}
        </button>
      ) : null}

      <p className={styles.runtimeNote}>
        Pilot’s in-chat “Allow once” prompts are run-scoped and remain in that
        conversation. They are not these durable action approvals.
      </p>
    </section>
  );
}
