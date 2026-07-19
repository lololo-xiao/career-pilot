"use client";

import { useEffect, useReducer, useState } from "react";

import {
  createSettingsDraftHydration,
  createSettingsReadController,
  initialRecoverableReadState,
  isAgentIdentity,
  nextReadGeneration,
  recoverableReadReducer,
  startRecoverableSettingsRead,
} from "./settings-recovery";
import type { AgentIdentity } from "./types";


interface AgentIdentitySettingsProps {
  apiBaseUrl: string;
}

async function readError(response: Response, fallback: string): Promise<string> {
  const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
  return payload?.detail ?? fallback;
}

export function AgentIdentitySettings({ apiBaseUrl }: AgentIdentitySettingsProps) {
  const [identity, setIdentity] = useState<AgentIdentity | null>(null);
  const [loadRetry, setLoadRetry] = useState(0);
  const [loadController] = useState(
    () => createSettingsReadController("identity"),
  );
  const [draftHydration] = useState(createSettingsDraftHydration);
  const [loadState, dispatchLoad] = useReducer(
    recoverableReadReducer<AgentIdentity>,
    undefined,
    () => initialRecoverableReadState<AgentIdentity>(),
  );
  const [name, setName] = useState("");
  const [soul, setSoul] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const abortController = new AbortController();
    const read = startRecoverableSettingsRead({
      apiBaseUrl,
      controller: loadController,
      fallback: "Agent identity could not load.",
      onFailure(error, generation) {
        dispatchLoad({ type: "failure", generation, error });
      },
      onStart(generation) {
        dispatchLoad({ type: "start", generation });
      },
      onSuccess(payload, generation) {
        setIdentity(payload);
        draftHydration.hydrate(() => {
          setName(payload.name);
          setSoul(payload.soul);
        });
        dispatchLoad({ type: "success", generation, data: payload });
      },
      signal: abortController.signal,
      validate: isAgentIdentity,
    });
    void read.completion;
    return () => {
      abortController.abort();
      loadController.invalidate(read.ticket);
    };
  }, [apiBaseUrl, draftHydration, loadController, loadRetry]);

  async function save() {
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const response = await fetch(`${apiBaseUrl}/settings/agent-identity`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, soul }),
      });
      if (!response.ok) throw new Error(await readError(response, "Agent identity could not be saved."));
      const payload = (await response.json()) as AgentIdentity;
      setIdentity(payload);
      setName(payload.name);
      setSoul(payload.soul);
      setNotice(`${payload.name}’s identity is saved locally and will survive restarts. Continue to Memories & Skills when you’re ready.`);
    } catch (caughtError) {
      setError(caughtError instanceof Error ? caughtError.message : "Agent identity could not be saved.");
    } finally {
      setSaving(false);
    }
  }

  const dirty = Boolean(identity && (identity.name !== name.trim() || identity.soul !== soul.trim()));

  return (
    <section
      className="auth-panel agent-identity-panel"
      id="settings-step-identity"
      aria-labelledby="agent-identity-title"
    >
      <div className="auth-panel-heading">
        <span className="eyebrow">Identity and soul</span>
        <h2 id="agent-identity-title" tabIndex={-1}>Make the companion yours</h2>
        <p>The name and personality notes are stored in the local database and mirrored to readable files in the agent workspace.</p>
      </div>
      {identity ? (
        <div className="agent-identity-form">
          <label>
            Agent name
            <input maxLength={80} onChange={(event) => setName(event.target.value)} value={name} />
          </label>
          <label>
            Soul notes
            <textarea
              maxLength={32768}
              onChange={(event) => setSoul(event.target.value)}
              placeholder="Describe the tone, personality, and collaboration style you want. Core safety and truthfulness rules remain protected."
              rows={7}
              value={soul}
            />
          </label>
          <div className="agent-resource-actions">
            <p>These notes shape personality and tone. They cannot override approval, safety, or evidence rules.</p>
            <button className="auth-action" disabled={saving || !dirty || !name.trim()} onClick={() => void save()} type="button">
              {saving ? "Saving…" : "Save identity"}
            </button>
          </div>
        </div>
      ) : loadState.status === "loading" ? (
        <p className="settings-loading" role="status">Loading agent identity…</p>
      ) : null}
      {notice ? <div className="capability-notice" role="status">{notice}</div> : null}
      {loadState.error ? (
        <div className="settings-recovery-error" role="alert">
          <div>
            <strong>Agent identity unavailable</strong>
            <span>{loadState.error} Unsaved name and soul notes are preserved.</span>
          </div>
          <button
            className="secondary-action"
            disabled={loadState.status === "loading"}
            onClick={() => setLoadRetry(nextReadGeneration)}
            type="button"
          >
            {loadState.status === "loading" ? "Retrying…" : "Retry identity"}
          </button>
        </div>
      ) : null}
      {error ? <div className="auth-error" role="alert"><strong>Agent identity</strong><span>{error}</span></div> : null}
    </section>
  );
}
