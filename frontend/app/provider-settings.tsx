"use client";

import { FormEvent, useEffect, useReducer, useRef, useState } from "react";

import {
  createSettingsReadController,
  initialRecoverableReadState,
  isProviderSettingsResponse,
  nextReadGeneration,
  recoverableReadReducer,
  startRecoverableSettingsRead,
} from "./settings-recovery";
import {
  createCodexAttemptStorageLifecycle,
  isCodexLoginStartResponse,
  type CodexAttemptStorageBinding,
  type CodexAttemptStorageLifecycle,
} from "./provider-login-storage";
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
  user: AuthUser | null;
  onUpdated: (user: AuthUser) => void;
}

interface ProviderCodexState {
  attemptsByUser: Record<string, CodexLoginStartResponse | null>;
  storageBinding: CodexAttemptStorageBinding | null;
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
  const activeUserId = user?.id ?? null;
  const [settings, setSettings] = useState<ProviderSettingsResponse | null>(null);
  const [loadRetry, setLoadRetry] = useState(0);
  const [loadController] = useState(
    () => createSettingsReadController("providers"),
  );
  const [loadState, dispatchLoad] = useReducer(
    recoverableReadReducer<ProviderSettingsResponse>,
    undefined,
    () => initialRecoverableReadState<ProviderSettingsResponse>(),
  );
  const [apiKey, setApiKey] = useState("");
  const [apiKeyLoading, setApiKeyLoading] = useState(false);
  const [codexStartingUserId, setCodexStartingUserId] =
    useState<string | null>(null);
  const [actionProvider, setActionProvider] = useState<ProviderMethod | null>(null);
  const [codexState, setCodexState] = useState<ProviderCodexState>({
    attemptsByUser: {},
    storageBinding: null,
  });
  const codexAttempt = activeUserId
    ? codexState.attemptsByUser[activeUserId] ?? null
    : null;
  const codexLoading = Boolean(activeUserId)
    && (
      Boolean(codexAttempt)
      || codexStartingUserId === activeUserId
    );
  const storageLifecycleRef = useRef<CodexAttemptStorageLifecycle | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loginWindowRef = useRef<Window | null>(null);

  useEffect(() => {
    const abortController = new AbortController();
    const read = startRecoverableSettingsRead({
      apiBaseUrl,
      controller: loadController,
      fallback: "Provider settings could not load.",
      onFailure(error, generation) {
        dispatchLoad({ type: "failure", generation, error });
      },
      onStart(generation) {
        dispatchLoad({ type: "start", generation });
      },
      onSuccess(payload, generation) {
        setSettings(payload);
        dispatchLoad({ type: "success", generation, data: payload });
      },
      signal: abortController.signal,
      validate: isProviderSettingsResponse,
    });
    void read.completion;
    return () => {
      abortController.abort();
      loadController.invalidate(read.ticket);
    };
  }, [apiBaseUrl, loadController, loadRetry]);

  useEffect(() => {
    let active = true;
    if (!activeUserId) {
      window.queueMicrotask(() => {
        if (!active) return;
        setCodexState((current) => ({
          ...current,
          storageBinding: null,
        }));
      });
      return () => {
        active = false;
      };
    }
    const lifecycle = storageLifecycleRef.current
      ?? createCodexAttemptStorageLifecycle(window.sessionStorage);
    storageLifecycleRef.current = lifecycle;
    const binding = lifecycle.bind(activeUserId);
    window.queueMicrotask(() => {
      if (!active) return;
      setCodexState((current) => ({
        attemptsByUser: {
          ...current.attemptsByUser,
          [activeUserId]: binding?.attempt ?? null,
        },
        storageBinding: binding,
      }));
      setCopied(false);
    });
    return () => {
      active = false;
    };
  }, [activeUserId]);

  useEffect(() => {
    if (
      !activeUserId
      || codexState.storageBinding?.userId !== activeUserId
      || !storageLifecycleRef.current
    ) return;
    const attempt = codexState.attemptsByUser[activeUserId] ?? null;
    storageLifecycleRef.current.persist(codexState.storageBinding, attempt);
  }, [activeUserId, codexState]);

  useEffect(() => {
    if (!codexAttempt || !activeUserId) return;
    const attemptUserId = activeUserId;
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
          setCodexState((current) => ({
            ...current,
            attemptsByUser: {
              ...current.attemptsByUser,
              [attemptUserId]: null,
            },
          }));
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
          setCodexState((current) => ({
            ...current,
            attemptsByUser: {
              ...current.attemptsByUser,
              [attemptUserId]: null,
            },
          }));
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
        setCodexState((current) => ({
          ...current,
          attemptsByUser: {
            ...current.attemptsByUser,
            [attemptUserId]: null,
          },
        }));
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
  }, [activeUserId, apiBaseUrl, codexAttempt, onUpdated]);

  async function connectApiKey(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!activeUserId) {
      setError("Wait for the local session to finish loading before connecting a key.");
      return;
    }
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
    if (!activeUserId) {
      setError("Wait for the local session to finish loading before changing connections.");
      return;
    }
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
    if (!activeUserId) {
      setError("Wait for the local session to finish loading before removing connections.");
      return;
    }
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
      const nextSettings = (await response.json()) as ProviderSettingsResponse;
      setSettings(nextSettings);
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
    if (!activeUserId) {
      setError("Wait for the local session to finish loading before connecting ChatGPT.");
      return;
    }
    const actionUserId = activeUserId;
    setError(null);
    setCopied(false);
    setCodexStartingUserId(actionUserId);
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
      const payload = (await response.json()) as unknown;
      if (!isCodexLoginStartResponse(payload)) {
        throw new Error("CareerPilot returned an invalid ChatGPT login attempt.");
      }
      setCodexState((current) => ({
        ...current,
        attemptsByUser: {
          ...current.attemptsByUser,
          [actionUserId]: payload,
        },
      }));
      setCodexStartingUserId((current) =>
        current === actionUserId ? null : current
      );
    } catch (caughtError) {
      setCodexStartingUserId((current) =>
        current === actionUserId ? null : current
      );
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
    if (!activeUserId || !attempt) return;
    loginWindowRef.current?.close();
    loginWindowRef.current = null;
    setCodexState((current) => ({
      ...current,
      attemptsByUser: {
        ...current.attemptsByUser,
        [activeUserId]: null,
      },
    }));
    setCodexStartingUserId((current) =>
      current === activeUserId ? null : current
    );
    setCopied(false);
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
    <section
      className="auth-panel settings-panel"
      id="settings-step-ai"
      aria-labelledby="provider-title"
    >
      <div className="auth-panel-heading">
        <span className="eyebrow">AI connection</span>
        <h2 id="provider-title" tabIndex={-1}>Choose how CareerPilot runs</h2>
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
                <button
                  disabled={!activeUserId}
                  type="button"
                  onClick={() => void cancelCodexLogin()}
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="connection-actions">
              {codex?.connected && !codex.active ? (
                <button
                  className="auth-action auth-action-dark"
                  type="button"
                  disabled={!activeUserId || actionProvider !== null || codexLoading}
                  onClick={() => void selectProvider("codex")}
                >
                  {actionProvider === "codex" ? "Selecting…" : "Use this connection"}
                </button>
              ) : null}
              {!codex?.active ? (
                <button
                  className="auth-action secondary-action"
                  type="button"
                  disabled={!activeUserId || !settings || actionProvider !== null || codexLoading}
                  onClick={() => void startCodexLogin()}
                >
                  {codexLoading ? "Starting…" : codex?.connected ? "Reconnect" : "Connect ChatGPT"}
                </button>
              ) : null}
              {codex?.connected ? (
                <button
                  className="text-action danger-action"
                  type="button"
                  disabled={!activeUserId || actionProvider !== null}
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
                disabled={!settings || apiKeyLoading || codexLoading}
                onChange={(event) => setApiKey(event.target.value)}
              />
              <button
                className="auth-action auth-action-light"
                type="submit"
                disabled={!activeUserId || !settings || apiKeyLoading || codexLoading || apiKey.trim().length < 20}
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
                  disabled={!activeUserId || actionProvider !== null}
                  onClick={() => void selectProvider("api_key")}
                >
                  {actionProvider === "api_key" ? "Selecting…" : "Use this connection"}
                </button>
              ) : null}
              <button
                className="text-action danger-action"
                type="button"
                disabled={!activeUserId || actionProvider !== null}
                onClick={() => void disconnectProvider("api_key")}
              >
                Remove
              </button>
            </div>
          ) : null}
        </div>
      </article>

      {!settings && loadState.status === "loading" ? (
        <p className="settings-loading" role="status">Loading connections…</p>
      ) : null}
      {loadState.error ? (
        <div className="settings-recovery-error" role="alert">
          <div>
            <strong>Connections unavailable</strong>
            <span>{loadState.error} Any key you typed is still here.</span>
          </div>
          <button
            className="secondary-action"
            disabled={loadState.status === "loading"}
            onClick={() => setLoadRetry(nextReadGeneration)}
            type="button"
          >
            {loadState.status === "loading" ? "Retrying…" : "Retry connections"}
          </button>
        </div>
      ) : null}
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
