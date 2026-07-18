"use client";

import { FormEvent, useEffect, useRef, useState } from "react";

import type {
  AuthSessionResponse,
  AuthUser,
  CodexLoginStartResponse,
  CodexLoginStatusResponse,
  ProviderMethod,
  ProviderSettingsResponse,
} from "./types";


interface ProviderSettingsProps {
  apiBaseUrl: string;
  user: AuthUser;
  onUpdated: (user: AuthUser) => void;
}

async function readError(response: Response, fallback: string): Promise<string> {
  const payload = (await response.json().catch(() => null)) as
    | { detail?: string }
    | null;
  return payload?.detail ?? fallback;
}

export function ProviderSettings({
  apiBaseUrl,
  user,
  onUpdated,
}: ProviderSettingsProps) {
  const storageKey = `careerpilot_codex_login_attempt:${user.id}`;
  const [settings, setSettings] = useState<ProviderSettingsResponse | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [apiKeyLoading, setApiKeyLoading] = useState(false);
  const [codexLoading, setCodexLoading] = useState(false);
  const [actionProvider, setActionProvider] = useState<ProviderMethod | null>(null);
  const [codexAttempt, setCodexAttempt] =
    useState<CodexLoginStartResponse | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loginWindowRef = useRef<Window | null>(null);

  useEffect(() => {
    let active = true;
    async function loadSettings() {
      try {
        const response = await fetch(`${apiBaseUrl}/settings/providers`, {
          credentials: "include",
        });
        if (!response.ok) {
          throw new Error(await readError(response, "Provider settings could not load."));
        }
        const payload = (await response.json()) as ProviderSettingsResponse;
        if (active) setSettings(payload);
      } catch (caughtError) {
        if (active) {
          setError(
            caughtError instanceof Error
              ? caughtError.message
              : "Provider settings could not load.",
          );
        }
      }
    }
    void loadSettings();
    return () => {
      active = false;
    };
  }, [apiBaseUrl]);

  useEffect(() => {
    let active = true;
    window.queueMicrotask(() => {
      if (!active) return;
      try {
        const rawAttempt = window.sessionStorage.getItem(storageKey);
        if (!rawAttempt) return;
        const attempt = JSON.parse(rawAttempt) as CodexLoginStartResponse;
        if (attempt.expires_at > Date.now() / 1000) {
          setCodexAttempt(attempt);
          setCodexLoading(true);
        } else {
          window.sessionStorage.removeItem(storageKey);
        }
      } catch {
        window.sessionStorage.removeItem(storageKey);
      }
    });
    return () => {
      active = false;
    };
  }, [storageKey]);

  useEffect(() => {
    try {
      if (codexAttempt) {
        window.sessionStorage.setItem(storageKey, JSON.stringify(codexAttempt));
      } else {
        window.sessionStorage.removeItem(storageKey);
      }
    } catch {
      // The active tab still retains the attempt if session storage is unavailable.
    }
  }, [codexAttempt, storageKey]);

  useEffect(() => {
    if (!codexAttempt) return;
    let active = true;
    let timer: number | undefined;

    async function poll() {
      try {
        const response = await fetch(
          `${apiBaseUrl}/settings/providers/codex/status/${codexAttempt?.attempt_id}`,
          { method: "POST", credentials: "include" },
        );
        if (!response.ok) {
          throw new Error(
            await readError(response, "CareerPilot could not finish ChatGPT login."),
          );
        }
        const payload = (await response.json()) as CodexLoginStatusResponse;
        if (!active) return;
        if (payload.status === "completed" && payload.user) {
          loginWindowRef.current?.close();
          loginWindowRef.current = null;
          setCodexAttempt(null);
          setCodexLoading(false);
          setSettings((current) => current ? {
            active_provider: "codex",
            connections: current.connections.map((connection) =>
              connection.provider === "codex"
                ? {
                    ...connection,
                    connected: true,
                    active: true,
                    provider_email: payload.user?.provider_email ?? null,
                    plan_type: payload.user?.plan_type ?? null,
                  }
                : { ...connection, active: false },
            ),
          } : current);
          onUpdated(payload.user);
          return;
        }
        if (payload.status === "failed" || payload.status === "expired") {
          loginWindowRef.current?.close();
          loginWindowRef.current = null;
          setCodexAttempt(null);
          setCodexLoading(false);
          setError(
            payload.error ??
              (payload.status === "expired"
                ? "The ChatGPT sign-in code expired. Start a new connection."
                : "ChatGPT login did not complete."),
          );
          return;
        }
        timer = window.setTimeout(poll, 1800);
      } catch (caughtError) {
        if (!active) return;
        loginWindowRef.current?.close();
        loginWindowRef.current = null;
        setCodexAttempt(null);
        setCodexLoading(false);
        setError(
          caughtError instanceof Error
            ? caughtError.message
            : "CareerPilot could not finish ChatGPT login.",
        );
      }
    }

    timer = window.setTimeout(poll, 800);
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [apiBaseUrl, codexAttempt, onUpdated]);

  async function connectApiKey(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setApiKeyLoading(true);
    try {
      const response = await fetch(`${apiBaseUrl}/settings/providers/api-key`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: apiKey.trim() }),
      });
      if (!response.ok) {
        throw new Error(await readError(response, "OpenAI rejected this API key."));
      }
      const payload = (await response.json()) as AuthSessionResponse;
      if (!payload.user) throw new Error("CareerPilot could not activate this key.");
      setApiKey("");
      setSettings((current) => current ? {
        active_provider: "api_key",
        connections: current.connections.map((connection) =>
          connection.provider === "api_key"
            ? { ...connection, connected: true, active: true, plan_type: "usage-based" }
            : { ...connection, active: false },
        ),
      } : current);
      onUpdated(payload.user);
    } catch (caughtError) {
      setError(
        caughtError instanceof Error
          ? caughtError.message
          : "CareerPilot could not connect this API key.",
      );
    } finally {
      setApiKeyLoading(false);
    }
  }

  async function selectProvider(provider: ProviderMethod) {
    setError(null);
    setActionProvider(provider);
    try {
      const response = await fetch(`${apiBaseUrl}/settings/providers/select`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider }),
      });
      if (!response.ok) {
        throw new Error(await readError(response, "This connection could not be selected."));
      }
      const payload = (await response.json()) as AuthSessionResponse;
      if (!payload.user) throw new Error("CareerPilot could not select this connection.");
      setSettings((current) => current ? {
        active_provider: provider,
        connections: current.connections.map((connection) => ({
          ...connection,
          active: connection.provider === provider,
        })),
      } : current);
      onUpdated(payload.user);
    } catch (caughtError) {
      setError(
        caughtError instanceof Error
          ? caughtError.message
          : "This connection could not be selected.",
      );
    } finally {
      setActionProvider(null);
    }
  }

  async function disconnectProvider(provider: ProviderMethod) {
    setError(null);
    setActionProvider(provider);
    try {
      const response = await fetch(`${apiBaseUrl}/settings/providers/${provider}`, {
        method: "DELETE",
        credentials: "include",
      });
      if (!response.ok) {
        throw new Error(await readError(response, "This connection could not be removed."));
      }
      setSettings((await response.json()) as ProviderSettingsResponse);
      const sessionResponse = await fetch(`${apiBaseUrl}/local/session`);
      const session = (await sessionResponse.json()) as AuthSessionResponse;
      if (session.user) onUpdated(session.user);
    } catch (caughtError) {
      setError(
        caughtError instanceof Error
          ? caughtError.message
          : "This connection could not be removed.",
      );
    } finally {
      setActionProvider(null);
    }
  }

  async function startCodexLogin() {
    setError(null);
    setCopied(false);
    setCodexLoading(true);
    try {
      const response = await fetch(`${apiBaseUrl}/settings/providers/codex/start`, {
        method: "POST",
        credentials: "include",
      });
      if (!response.ok) {
        throw new Error(
          await readError(response, "CareerPilot could not start ChatGPT login."),
        );
      }
      setCodexAttempt((await response.json()) as CodexLoginStartResponse);
    } catch (caughtError) {
      setCodexLoading(false);
      setError(
        caughtError instanceof Error
          ? caughtError.message
          : "CareerPilot could not start ChatGPT login.",
      );
    }
  }

  function openCodexLoginWindow() {
    if (!codexAttempt) return;
    setError(null);
    const loginWindow = window.open(
      codexAttempt.verification_url,
      "careerpilot-chatgpt-login",
      "popup=yes,width=620,height=760,resizable=yes,scrollbars=yes",
    );
    if (!loginWindow) {
      setError("Allow pop-ups for CareerPilot, then open ChatGPT again.");
      return;
    }
    loginWindowRef.current = loginWindow;
    loginWindow.focus();
  }

  async function cancelCodexLogin() {
    const attempt = codexAttempt;
    loginWindowRef.current?.close();
    loginWindowRef.current = null;
    setCodexAttempt(null);
    setCodexLoading(false);
    setCopied(false);
    if (!attempt) return;
    try {
      const response = await fetch(
        `${apiBaseUrl}/settings/providers/codex/attempts/${attempt.attempt_id}`,
        { method: "DELETE", credentials: "include" },
      );
      if (!response.ok) {
        throw new Error("CareerPilot could not cancel this connection attempt.");
      }
    } catch (caughtError) {
      setError(
        caughtError instanceof Error
          ? caughtError.message
          : "CareerPilot could not cancel this connection attempt.",
      );
    }
  }

  const codex = settings?.connections.find((item) => item.provider === "codex");
  const key = settings?.connections.find((item) => item.provider === "api_key");

  return (
    <section className="auth-panel settings-panel" aria-labelledby="provider-title">
      <div className="auth-panel-heading">
        <span className="eyebrow">AI connection</span>
        <h2 id="provider-title">Choose how CareerPilot runs</h2>
        <p>
          This powers Pilot’s conversations and fit checks. A connection is required
          before the local workspace can use AI features.
        </p>
      </div>

      <article className={`auth-option auth-option-primary${codex?.active ? " is-active" : ""}`}>
        <div className="auth-option-icon" aria-hidden="true">◈</div>
        <div>
          <div className="auth-option-title">
            <h3>ChatGPT plan through Codex</h3>
            <span>{codex?.active ? "Active" : "Plan-backed"}</span>
          </div>
          <p>
            Requires a compatible Codex CLI/runtime already installed on this device.
            Authorize it here to use limits included with an eligible ChatGPT plan.
          </p>
          {codex?.connected ? (
            <p className="connection-detail">
              Connected{codex.provider_email ? ` as ${codex.provider_email}` : ""}
              {codex.plan_type ? ` · ${codex.plan_type} plan` : ""}
            </p>
          ) : null}

          {codexAttempt ? (
            <div className="device-code-panel" aria-live="polite">
              <span>Enter this one-time code</span>
              <button
                type="button"
                className="device-code"
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(codexAttempt.user_code);
                    setCopied(true);
                  } catch {
                    setError("Select the code and copy it manually.");
                  }
                }}
              >
                {codexAttempt.user_code}
                <small>{copied ? "Copied" : "Copy"}</small>
              </button>
              <button
                type="button"
                className="auth-action auth-action-dark"
                onClick={openCodexLoginWindow}
              >
                Open ChatGPT in a separate window <span aria-hidden="true">↗</span>
              </button>
              <div className="device-code-status">
                <small>Keep this page open. Connection continues after approval.</small>
                <button type="button" onClick={() => void cancelCodexLogin()}>Cancel</button>
              </div>
            </div>
          ) : (
            <div className="connection-actions">
              {codex?.connected && !codex.active ? (
                <button
                  className="auth-action auth-action-dark"
                  type="button"
                  disabled={actionProvider !== null || codexLoading}
                  onClick={() => void selectProvider("codex")}
                >
                  {actionProvider === "codex" ? "Selecting…" : "Use this connection"}
                </button>
              ) : null}
              {!codex?.active ? (
                <button
                  className="auth-action secondary-action"
                  type="button"
                  disabled={actionProvider !== null || codexLoading}
                  onClick={() => void startCodexLogin()}
                >
                  {codexLoading ? "Starting…" : codex?.connected ? "Reconnect" : "Connect ChatGPT"}
                </button>
              ) : null}
              {codex?.connected ? (
                <button
                  className="text-action danger-action"
                  type="button"
                  disabled={actionProvider !== null}
                  onClick={() => void disconnectProvider("codex")}
                >
                  Remove
                </button>
              ) : null}
            </div>
          )}
        </div>
      </article>

      <div className="auth-divider"><span>or</span></div>

      <article className={`auth-option${key?.active ? " is-active" : ""}`}>
        <div className="auth-option-icon auth-option-icon-key" aria-hidden="true">⌁</div>
        <div>
          <div className="auth-option-title">
            <h3>OpenAI API key</h3>
            <span>{key?.active ? "Active" : "Usage-based"}</span>
          </div>
          <p>
            Use an OpenAI Platform project with separate usage billing and API
            administration.
          </p>
          {key?.connected ? <p className="connection-detail">API key securely stored</p> : null}
          <form className="api-key-form" onSubmit={connectApiKey}>
            <label htmlFor="openai-api-key">
              {key?.connected ? "Replace API key" : "OpenAI API key"}
            </label>
            <div className="api-key-row">
              <input
                id="openai-api-key"
                type="password"
                value={apiKey}
                minLength={20}
                maxLength={512}
                required
                autoComplete="off"
                spellCheck={false}
                placeholder="sk-…"
                disabled={apiKeyLoading || codexLoading}
                onChange={(event) => setApiKey(event.target.value)}
              />
              <button
                className="auth-action auth-action-light"
                type="submit"
                disabled={apiKeyLoading || codexLoading || apiKey.trim().length < 20}
              >
                {apiKeyLoading ? "Verifying…" : key?.connected ? "Replace" : "Connect key"}
              </button>
            </div>
          </form>
          {key?.connected ? (
            <div className="connection-actions">
              {!key.active ? (
                <button
                  className="auth-action auth-action-dark"
                  type="button"
                  disabled={actionProvider !== null}
                  onClick={() => void selectProvider("api_key")}
                >
                  {actionProvider === "api_key" ? "Selecting…" : "Use this connection"}
                </button>
              ) : null}
              <button
                className="text-action danger-action"
                type="button"
                disabled={actionProvider !== null}
                onClick={() => void disconnectProvider("api_key")}
              >
                Remove
              </button>
            </div>
          ) : null}
        </div>
      </article>

      {!settings && !error ? <p className="settings-loading">Loading connections…</p> : null}
      {error ? (
        <div className="auth-error" role="alert">
          <strong>Connection unavailable</strong>
          <span>{error}</span>
        </div>
      ) : null}
      <p className="auth-fine-print">
        ChatGPT subscriptions and OpenAI API billing remain separate. Credentials are
        encrypted before storage.
      </p>
    </section>
  );
}
