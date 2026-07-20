"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { API_BASE_URL } from "../api-base-url";
import styles from "./discovery.module.css";
import {
  jobKey,
  jobWritePayload,
  normalizeCompanyIdentifier,
  PreviewRequestCoordinator,
  parseJobWriteResult,
  parseLocalSessionResponse,
  parsePublicDiscoveryResponse,
  previewBindingMatches,
  saveJobSnapshotSequentially,
  snapshotSelectedJobs,
  type DiscoveryProvider,
  type PreviewBinding,
  type PublicDiscoveryResponse,
  type SaveOutcome,
} from "./discovery";


interface BoundPreview {
  binding: PreviewBinding;
  response: PublicDiscoveryResponse;
}

async function responseError(response: Response, fallback: string): Promise<string> {
  const payload: unknown = await response.json().catch(() => null);
  if (
    typeof payload === "object"
    && payload !== null
    && "detail" in payload
    && typeof payload.detail === "string"
    && payload.detail.length <= 1_000
  ) return payload.detail;
  return fallback;
}

async function readSession() {
  const response = await fetch(`${API_BASE_URL}/local/session`, {
    credentials: "include",
    cache: "no-store",
  });
  if (!response.ok) throw new Error("Your local account could not be checked.");
  return parseLocalSessionResponse(await response.json());
}

export default function GuidedJobDiscovery() {
  const router = useRouter();
  const requestCoordinator = useRef(new PreviewRequestCoordinator());
  const mounted = useRef(true);
  const [accountId, setAccountId] = useState<string | null>(null);
  const [checkingSession, setCheckingSession] = useState(true);
  const [provider, setProvider] = useState<DiscoveryProvider>("greenhouse");
  const [companyIdentifier, setCompanyIdentifier] = useState("");
  const [limit, setLimit] = useState(10);
  const [preview, setPreview] = useState<BoundPreview | null>(null);
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set());
  const [saveOutcomes, setSaveOutcomes] = useState<Record<string, SaveOutcome>>({});
  const [previewing, setPreviewing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  const clearPreview = useCallback((message?: string) => {
    requestCoordinator.current.invalidate();
    setPreview(null);
    setSelectedKeys(new Set());
    setSaveOutcomes({});
    setPreviewing(false);
    setError(null);
    setStatus(message ?? null);
  }, []);

  useEffect(() => {
    const coordinator = requestCoordinator.current;
    mounted.current = true;
    async function open() {
      try {
        const session = await readSession();
        if (!mounted.current) return;
        if (!session.authenticated || !session.user) {
          router.replace("/");
          return;
        }
        if (!session.user.active_provider) {
          router.replace("/settings");
          return;
        }
        setAccountId(session.user.id);
      } catch (caught) {
        if (mounted.current) {
          setError(caught instanceof Error ? caught.message : "Your local account could not be checked.");
        }
      } finally {
        if (mounted.current) setCheckingSession(false);
      }
    }
    void open();
    return () => {
      mounted.current = false;
      coordinator.invalidate();
    };
  }, [router]);

  useEffect(() => {
    async function recheckOnFocus() {
      if (!accountId || saving) return;
      try {
        const session = await readSession();
        const nextAccountId = session.authenticated && session.user?.active_provider
          ? session.user.id
          : null;
        if (nextAccountId !== accountId) {
          clearPreview(
            "The local account changed, so the previous preview and selection were reset.",
          );
          setAccountId(nextAccountId);
          if (!nextAccountId) router.replace("/settings");
        }
      } catch {
        // The mandatory pre-save check remains authoritative if a focus check fails.
      }
    }
    window.addEventListener("focus", recheckOnFocus);
    return () => window.removeEventListener("focus", recheckOnFocus);
  }, [accountId, clearPreview, router, saving]);

  function changeProvider(nextProvider: DiscoveryProvider) {
    if (nextProvider === provider) return;
    clearPreview();
    setProvider(nextProvider);
  }

  function changeIdentifier(value: string) {
    if (value === companyIdentifier) return;
    clearPreview();
    setCompanyIdentifier(value);
  }

  function changeLimit(value: number) {
    const bounded = Math.max(1, Math.min(25, value || 1));
    if (bounded === limit) return;
    clearPreview();
    setLimit(bounded);
  }

  async function requestPreview(event: FormEvent) {
    event.preventDefault();
    if (!accountId || previewing || saving) return;
    const identifier = normalizeCompanyIdentifier(companyIdentifier);
    if (!identifier) {
      setError("Enter one public board token or company slug.");
      return;
    }

    const ticket = requestCoordinator.current.start();
    setPreview(null);
    setSelectedKeys(new Set());
    setSaveOutcomes({});
    setPreviewing(true);
    setError(null);
    setStatus("Reading the selected public provider feed…");
    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/jobs/discover-public`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, company_identifier: identifier, limit }),
        signal: ticket.signal,
      });
      if (!response.ok) {
        throw new Error(await responseError(response, "The public job preview could not be loaded."));
      }
      const payload = parsePublicDiscoveryResponse(
        await response.json(),
        provider,
        identifier,
        limit,
      );
      if (!ticket.isCurrent() || !mounted.current) return;
      setPreview({
        binding: {
          accountId,
          provider,
          companyIdentifier: identifier,
          generation: ticket.generation,
        },
        response: payload,
      });
      setSelectedKeys(new Set());
      setStatus(
        `${payload.returned} of ${payload.discovered} public roles previewed. Zero were selected or saved.`,
      );
    } catch (caught) {
      if (!ticket.isCurrent() || (caught instanceof DOMException && caught.name === "AbortError")) return;
      setError(caught instanceof Error ? caught.message : "The public job preview could not be loaded.");
      setStatus(null);
    } finally {
      if (ticket.isCurrent() && mounted.current) {
        setPreviewing(false);
        requestCoordinator.current.finish(ticket.generation);
      }
    }
  }

  function cancelPreview() {
    clearPreview("Preview request cancelled. It made no local save changes.");
  }

  function toggleJob(key: string, checked: boolean) {
    setSelectedKeys((current) => {
      const next = new Set(current);
      if (checked) next.add(key);
      else next.delete(key);
      return next;
    });
    setSaveOutcomes((current) => {
      if (!current[key]) return current;
      const next = { ...current };
      delete next[key];
      return next;
    });
  }

  async function saveSelectedJobs() {
    if (!accountId || !preview || saving || selectedKeys.size === 0) return;
    if (!previewBindingMatches(
      preview.binding,
      accountId,
      provider,
      companyIdentifier,
      requestCoordinator.current.currentGeneration(),
    )) {
      clearPreview("The preview inputs changed, so the previous selection was reset.");
      return;
    }

    const frozenSnapshot = snapshotSelectedJobs(preview.response.jobs, selectedKeys);
    setSaving(true);
    setError(null);
    setStatus(`Saving ${frozenSnapshot.length} checked role${frozenSnapshot.length === 1 ? "" : "s"} in order…`);
    try {
      const result = await saveJobSnapshotSequentially(
        frozenSnapshot,
        accountId,
        async () => {
          const session = await readSession();
          return session.authenticated && session.user?.active_provider ? session.user.id : null;
        },
        async (job) => {
          const response = await fetch(`${API_BASE_URL}/api/v1/jobs`, {
            method: "POST",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(jobWritePayload(job)),
          });
          if (!response.ok) {
            throw new Error(await responseError(response, "Save not confirmed — retry safely"));
          }
          return parseJobWriteResult(await response.json());
        },
        (outcome) => {
          if (!mounted.current) return;
          setSaveOutcomes((current) => ({ ...current, [outcome.key]: outcome }));
          if (outcome.state !== "failed") {
            setSelectedKeys((current) => {
              const next = new Set(current);
              next.delete(outcome.key);
              return next;
            });
          }
        },
      );

      if (!mounted.current) return;
      if (result.accountChanged) {
        clearPreview(
          "The local account changed, so discovery was reset. Jobs already confirmed before the reset remain saved in their original account.",
        );
        setAccountId(result.currentAccountId);
        if (!result.currentAccountId) router.replace("/settings");
        return;
      }
      const failed = result.outcomes.filter((outcome) => outcome.state === "failed").length;
      const confirmed = result.outcomes.length - failed;
      setStatus(
        failed
          ? `${confirmed} save${confirmed === 1 ? "" : "s"} confirmed; ${failed} remain selected for an idempotent retry.`
          : `${confirmed} save${confirmed === 1 ? "" : "s"} confirmed.`,
      );
    } catch (caught) {
      if (!mounted.current) return;
      setError(caught instanceof Error ? caught.message : "Saving stopped before every result was confirmed.");
      setStatus("Saving stopped. Confirmed jobs remain saved; checked jobs can be retried safely.");
    } finally {
      if (mounted.current) setSaving(false);
    }
  }

  if (checkingSession) {
    return <main className={styles.loading}><p>Opening guided job discovery…</p></main>;
  }

  return (
    <main className={styles.shell}>
      <header className={styles.header}>
        <Link className={styles.brand} href="/"><span>CP</span><strong>CareerPilot</strong></Link>
        <nav aria-label="Discovery navigation">
          <Link href="/">Talk with Pilot</Link>
          <Link href="/workspace">Job queue</Link>
        </nav>
      </header>

      <section className={styles.hero}>
        <span className={styles.eyebrow}>Known-board discovery</span>
        <h1>Find roles from one public company board.</h1>
        <p>Preview first, choose deliberately, then save only the roles you checked.</p>
      </section>

      <ol className={styles.steps}>
        <li className={styles.step}>
          <div className={styles.stepNumber} aria-hidden="true">1</div>
          <section aria-labelledby="discovery-source-heading">
            <span className={styles.kicker}>Choose one source</span>
            <h2 id="discovery-source-heading">Public provider and company</h2>
            <form onSubmit={(event) => void requestPreview(event)}>
              <fieldset className={styles.providerChoice} disabled={previewing || saving}>
                <legend>Public job provider</legend>
                <label className={provider === "greenhouse" ? styles.chosenProvider : ""}>
                  <input
                    checked={provider === "greenhouse"}
                    name="provider"
                    onChange={() => changeProvider("greenhouse")}
                    type="radio"
                    value="greenhouse"
                  />
                  <span><strong>Greenhouse</strong><small>Public board token</small></span>
                </label>
                <label className={provider === "lever" ? styles.chosenProvider : ""}>
                  <input
                    checked={provider === "lever"}
                    name="provider"
                    onChange={() => changeProvider("lever")}
                    type="radio"
                    value="lever"
                  />
                  <span><strong>Lever</strong><small>Public company slug</small></span>
                </label>
              </fieldset>

              <div className={styles.formRow}>
                <label>
                  <span>{provider === "greenhouse" ? "Board token" : "Company slug"}</span>
                  <input
                    autoComplete="off"
                    disabled={previewing || saving}
                    maxLength={100}
                    onChange={(event) => changeIdentifier(event.target.value)}
                    placeholder={provider === "greenhouse" ? "example-labs" : "example-company"}
                    required
                    value={companyIdentifier}
                  />
                </label>
                <label>
                  <span>Preview limit</span>
                  <input
                    disabled={previewing || saving}
                    max={25}
                    min={1}
                    onChange={(event) => changeLimit(Number(event.target.value))}
                    type="number"
                    value={limit}
                  />
                </label>
              </div>

              <aside className={styles.networkNotice} aria-label="Public network disclosure">
                <strong>A public network read occurs when you preview.</strong>
                <p>Your provider choice, public board or site identifier, and ordinary request metadata such as your IP address and request headers leave this device for Greenhouse or Lever. CareerPilot does not send your profile or workspace data.</p>
              </aside>

              <div className={styles.actions}>
                <button className={styles.primaryButton} disabled={!accountId || previewing || saving} type="submit">
                  {previewing ? "Reading public feed…" : "Preview public jobs"}
                </button>
                {previewing ? <button className={styles.secondaryButton} onClick={cancelPreview} type="button">Cancel preview</button> : null}
              </div>
            </form>
          </section>
        </li>

        <li className={styles.step}>
          <div className={styles.stepNumber} aria-hidden="true">2</div>
          <section aria-labelledby="discovery-preview-heading">
            <span className={styles.kicker}>Preview bounded results</span>
            <h2 id="discovery-preview-heading">Check only the roles you want</h2>
            {!preview ? (
              <p className={styles.empty}>No accepted preview yet. Results always begin with zero jobs selected.</p>
            ) : preview.response.jobs.length === 0 ? (
              <p className={styles.empty}>This public feed returned no usable roles. Nothing was stored.</p>
            ) : (
              <div className={styles.results}>
                <p className={styles.resultSummary}>
                  Showing {preview.response.returned} of {preview.response.discovered} roles · {preview.response.stored} saved during preview
                </p>
                {preview.response.jobs.map((job) => {
                  const key = jobKey(job);
                  const outcome = saveOutcomes[key];
                  return (
                    <article className={styles.jobCard} key={key}>
                      <label className={styles.jobChoice}>
                        <input
                          checked={selectedKeys.has(key)}
                          disabled={saving || outcome?.state === "saved" || outcome?.state === "already"}
                          onChange={(event) => toggleJob(key, event.target.checked)}
                          type="checkbox"
                        />
                        <span>
                          <strong>{job.title}</strong>
                          <small>{job.company} · {job.locations.join(" · ") || "Location not listed"}</small>
                        </span>
                      </label>
                      <p className={styles.description}>{job.description || "No description was supplied by this public feed."}</p>
                      <dl className={styles.jobMeta}>
                        <div><dt>Type</dt><dd>{job.employment_type}</dd></div>
                        <div><dt>Posted</dt><dd>{job.posted_date ?? "Not listed"}</dd></div>
                        <div><dt>Public URL</dt><dd>{job.source_url}</dd></div>
                      </dl>
                      {outcome ? (
                        <p className={outcome.state === "failed" ? styles.failedStatus : styles.savedStatus} role="status">
                          {outcome.state === "failed" ? `Not confirmed: ${outcome.message}` : outcome.message}
                        </p>
                      ) : null}
                    </article>
                  );
                })}
              </div>
            )}
          </section>
        </li>

        <li className={styles.step}>
          <div className={styles.stepNumber} aria-hidden="true">3</div>
          <section aria-labelledby="discovery-save-heading">
            <span className={styles.kicker}>Explicit local save</span>
            <h2 id="discovery-save-heading">Add checked roles to your queue</h2>
            <p className={styles.saveCopy}>Each checked role is saved sequentially through the normal deduplication path. This does not score roles, create applications, approve anything, tailor documents, message anyone, fill a browser, or submit forms.</p>
            <button
              className={styles.saveButton}
              disabled={!preview || selectedKeys.size === 0 || saving}
              onClick={() => void saveSelectedJobs()}
              type="button"
            >
              {saving
                ? "Saving checked roles…"
                : selectedKeys.size > 0 && Object.values(saveOutcomes).some((item) => item.state === "failed")
                  ? `Retry ${selectedKeys.size} checked role${selectedKeys.size === 1 ? "" : "s"}`
                  : `Save ${selectedKeys.size} checked role${selectedKeys.size === 1 ? "" : "s"}`}
            </button>
          </section>
        </li>
      </ol>

      <div className={styles.liveRegion} aria-live="polite">
        {error ? <p className={styles.error}>{error}</p> : null}
        {status ? <p className={styles.status}>{status}</p> : null}
      </div>

      <footer className={styles.footer}>
        <p>This is known-board discovery, not broad web search. CareerPilot remains a private single-user release; this page makes no public-hosting or remote-authentication claim.</p>
      </footer>
    </main>
  );
}
