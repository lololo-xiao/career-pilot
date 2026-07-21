"use client";

import { useEffect, useMemo, useReducer, useRef, useState } from "react";

import {
  createMCPProbeCompletionGuard,
  reconcileMCPToolDraft,
} from "./mcp-discovery";
import {
  createSettingsDraftHydration,
  createSettingsReadController,
  initialRecoverableReadState,
  isMCPProbeIntent,
  isMCPProbeResult,
  isMCPSettingsResponse,
  nextReadGeneration,
  recoverableReadReducer,
  startRecoverableSettingsRead,
} from "./settings-recovery";
import type {
  MCPProbeIntent,
  MCPProbeResult,
  MCPServerSettings,
  MCPSettingsResponse,
} from "./types";


interface MCPSettingsProps {
  apiBaseUrl: string;
  onUpdated?: () => void;
}

async function readError(response: Response, fallback: string): Promise<string> {
  const payload = (await response.json().catch(() => null)) as
    | { detail?: string | Array<{ msg?: string }> }
    | null;
  if (typeof payload?.detail === "string") return payload.detail;
  if (Array.isArray(payload?.detail)) {
    return payload.detail.map((item) => item.msg).filter(Boolean).join(" ") || fallback;
  }
  return fallback;
}

function emptyServer(): MCPServerSettings {
  return {
    name: "new-server",
    display_name: "New MCP server",
    description: "",
    transport: "stdio",
    command: "",
    args: [],
    url: null,
    tool_allowlist: [],
    forwarded_environment: [],
    environment: {},
    source_url: null,
    warning: null,
    enabled: false,
    preset: false,
    command_available: null,
  };
}

function listText(values: string[]): string {
  return values.join("\n");
}

function parseList(value: string): string[] {
  return [...new Set(value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean))];
}

function environmentText(values: Record<string, string>): string {
  return Object.entries(values).map(([name, value]) => `${name}=${value}`).join("\n");
}

function parseEnvironment(value: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const line of value.split("\n").map((item) => item.trim()).filter(Boolean)) {
    const separator = line.indexOf("=");
    if (separator < 1) throw new Error(`Environment line "${line}" must use NAME=value.`);
    const name = line.slice(0, separator).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) throw new Error(`Invalid environment variable name: ${name}`);
    result[name] = line.slice(separator + 1);
  }
  return result;
}

const PROBE_STATUS_LABELS: Record<MCPProbeResult["status"], string> = {
  configuration_issue: "Needs setup",
  denied: "Cancelled",
  policy_blocked: "Blocked safely",
  protocol_error: "Protocol issue",
  ready: "Ready",
  ready_no_tools: "Ready · no tools",
  timed_out: "Timed out",
  unreachable: "Unreachable",
};

function formattedProbeTime(value: string): string {
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.valueOf()) ? "previously" : timestamp.toLocaleString();
}

export function MCPSettings({ apiBaseUrl, onUpdated }: MCPSettingsProps) {
  const [settings, setSettings] = useState<MCPSettingsResponse | null>(null);
  const [loadRetry, setLoadRetry] = useState(0);
  const [loadController] = useState(
    () => createSettingsReadController("mcp"),
  );
  const [draftHydration] = useState(createSettingsDraftHydration);
  const [loadState, dispatchLoad] = useReducer(
    recoverableReadReducer<MCPSettingsResponse>,
    undefined,
    () => initialRecoverableReadState<MCPSettingsResponse>(),
  );
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState<MCPServerSettings>(emptyServer());
  const [args, setArgs] = useState("");
  const [tools, setTools] = useState("");
  const [forwardedEnvironment, setForwardedEnvironment] = useState("");
  const [environment, setEnvironment] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [probeGuard] = useState(createMCPProbeCompletionGuard);
  const probeTriggerRef = useRef<HTMLButtonElement | null>(null);
  const probeAbortRef = useRef<AbortController | null>(null);
  const [probeIntent, setProbeIntent] = useState<MCPProbeIntent | null>(null);
  const [probeResult, setProbeResult] = useState<MCPProbeResult | null>(null);
  const [probeStage, setProbeStage] = useState<"intent" | "probe" | null>(null);

  const selected = useMemo(
    () => settings?.servers.find((server) => server.name === selectedName) ?? null,
    [selectedName, settings],
  );
  const enabledCount = settings?.servers.filter((server) => server.enabled).length ?? 0;
  const draftToolNames = useMemo(() => new Set(parseList(tools)), [tools]);
  const lastProbe = selectedName ? settings?.probe_statuses[selectedName] ?? null : null;
  const allowedPresentTools = probeResult?.discovered_tools.filter((tool) => tool.allowed && tool.selectable) ?? [];
  const newDiscoveredTools = probeResult?.discovered_tools.filter((tool) => !tool.allowed) ?? [];
  const probeTargetIsSaved = useMemo(() => {
    if (!selected || creating || draft.transport !== selected.transport) return false;
    try {
      return (
        (draft.command?.trim() || null) === selected.command
        && (draft.url?.trim() || null) === selected.url
        && JSON.stringify(parseList(args)) === JSON.stringify(selected.args)
        && JSON.stringify(parseList(forwardedEnvironment)) === JSON.stringify(selected.forwarded_environment)
        && JSON.stringify(parseEnvironment(environment)) === JSON.stringify(selected.environment)
      );
    } catch {
      return false;
    }
  }, [args, creating, draft.command, draft.transport, draft.url, environment, forwardedEnvironment, selected]);

  function invalidateProbe() {
    probeGuard.invalidate();
    probeAbortRef.current?.abort();
    probeAbortRef.current = null;
    setProbeIntent(null);
    setProbeResult(null);
    setProbeStage(null);
  }

  function restoreProbeFocus() {
    window.requestAnimationFrame(() => probeTriggerRef.current?.focus());
  }

  function loadDraft(server: MCPServerSettings, isCreating = false) {
    invalidateProbe();
    setCreating(isCreating);
    setSelectedName(isCreating ? null : server.name);
    setDraft({ ...server, environment: { ...server.environment } });
    setArgs(listText(server.args));
    setTools(listText(server.tool_allowlist));
    setForwardedEnvironment(listText(server.forwarded_environment));
    setEnvironment(environmentText(server.environment));
    setError(null);
    setNotice(null);
  }

  function applySettings(payload: MCPSettingsResponse, preferredName?: string) {
    setSettings(payload);
    const next = payload.servers.find((server) => server.name === preferredName) ?? payload.servers[0] ?? null;
    if (next) loadDraft(next);
    else loadDraft(emptyServer(), true);
  }

  useEffect(() => {
    const abortController = new AbortController();
    const read = startRecoverableSettingsRead({
      apiBaseUrl,
      controller: loadController,
      fallback: "MCP settings could not load.",
      onFailure(error, generation) {
        dispatchLoad({ type: "failure", generation, error });
      },
      onStart(generation) {
        dispatchLoad({ type: "start", generation });
      },
      onSuccess(payload, generation) {
        setSettings(payload);
        draftHydration.hydrate(() => {
          const first = payload.servers.find((server) => server.name === "linkedin-search") ?? payload.servers[0];
          const initial = first ?? emptyServer();
          setCreating(!first);
          setSelectedName(first?.name ?? null);
          setDraft({ ...initial, environment: { ...initial.environment } });
          setArgs(listText(initial.args));
          setTools(listText(initial.tool_allowlist));
          setForwardedEnvironment(listText(initial.forwarded_environment));
          setEnvironment(environmentText(initial.environment));
        });
        dispatchLoad({ type: "success", generation, data: payload });
      },
      signal: abortController.signal,
      validate: isMCPSettingsResponse,
    });
    void read.completion;
    return () => {
      abortController.abort();
      loadController.invalidate(read.ticket);
      probeGuard.invalidate();
      probeAbortRef.current?.abort();
      probeAbortRef.current = null;
    };
  }, [apiBaseUrl, draftHydration, loadController, loadRetry, probeGuard]);

  async function save() {
    invalidateProbe();
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const parsedTools = parseList(tools);
      const payload = {
        name: draft.name,
        display_name: draft.display_name,
        description: draft.description,
        transport: draft.transport,
        command: draft.transport === "stdio" ? draft.command?.trim() || null : null,
        args: draft.transport === "stdio" ? parseList(args) : [],
        url: draft.transport === "http" ? draft.url?.trim() || null : null,
        tool_allowlist: parsedTools,
        forwarded_environment: parseList(forwardedEnvironment),
        environment: draft.transport === "stdio" ? parseEnvironment(environment) : {},
        source_url: draft.source_url,
        warning: draft.warning,
        enabled: draft.enabled,
      };
      const endpoint = creating
        ? `${apiBaseUrl}/settings/mcp`
        : `${apiBaseUrl}/settings/mcp/${encodeURIComponent(selectedName ?? "")}`;
      const response = await fetch(endpoint, {
        method: creating ? "POST" : "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error(await readError(response, "MCP server could not be saved."));
      const next = (await response.json()) as MCPSettingsResponse;
      applySettings(next, draft.name);
      setNotice(`${draft.display_name} saved. Pilot’s local runtime has been refreshed.`);
      onUpdated?.();
    } catch (caughtError) {
      setError(caughtError instanceof Error ? caughtError.message : "MCP server could not be saved.");
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!selected || !window.confirm(`Remove ${selected.display_name}?`)) return;
    invalidateProbe();
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const response = await fetch(`${apiBaseUrl}/settings/mcp/${encodeURIComponent(selected.name)}`, {
        method: "DELETE",
        credentials: "include",
      });
      if (!response.ok) throw new Error(await readError(response, "MCP server could not be removed."));
      applySettings((await response.json()) as MCPSettingsResponse);
      setNotice(`${selected.display_name} removed.`);
      onUpdated?.();
    } catch (caughtError) {
      setError(caughtError instanceof Error ? caughtError.message : "MCP server could not be removed.");
    } finally {
      setSaving(false);
    }
  }

  async function beginProbe() {
    if (!selected || creating) return;
    invalidateProbe();
    const ticket = probeGuard.begin(selected.name);
    const abortController = new AbortController();
    probeAbortRef.current = abortController;
    setProbeStage("intent");
    setError(null);
    setNotice(null);
    try {
      const response = await fetch(
        `${apiBaseUrl}/settings/mcp/${encodeURIComponent(selected.name)}/probe-intents`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: "{}",
          signal: abortController.signal,
        },
      );
      if (!response.ok) {
        throw new Error(await readError(response, "Connection review could not be prepared."));
      }
      const payload: unknown = await response.json();
      if (!isMCPProbeIntent(payload)) {
        throw new Error("Connection review returned an invalid response.");
      }
      probeGuard.commit(ticket, () => setProbeIntent(payload));
    } catch (caughtError) {
      if (caughtError instanceof DOMException && caughtError.name === "AbortError") return;
      probeGuard.commit(ticket, () => {
        setError(caughtError instanceof Error ? caughtError.message : "Connection review could not be prepared.");
      });
    } finally {
      probeGuard.commit(ticket, () => {
        if (probeAbortRef.current === abortController) probeAbortRef.current = null;
        setProbeStage(null);
      });
    }
  }

  async function decideProbe(decision: "approved" | "denied") {
    if (!selected || !probeIntent) return;
    const reviewedIntent = probeIntent;
    const ticket = probeGuard.begin(selected.name);
    const abortController = new AbortController();
    probeAbortRef.current?.abort();
    probeAbortRef.current = abortController;
    setProbeStage("probe");
    setError(null);
    try {
      const response = await fetch(
        `${apiBaseUrl}/settings/mcp/${encodeURIComponent(selected.name)}/probe-intents/${encodeURIComponent(reviewedIntent.approval_id)}/decision`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decision }),
          signal: abortController.signal,
        },
      );
      if (!response.ok) {
        throw new Error(await readError(response, "Connection check could not finish."));
      }
      const payload: unknown = await response.json();
      if (!isMCPProbeResult(payload)) {
        throw new Error("Connection check returned an invalid response.");
      }
      probeGuard.commit(ticket, () => {
        setProbeIntent(null);
        restoreProbeFocus();
        if (decision === "denied") {
          setProbeResult(null);
          setNotice("Connection check cancelled. Nothing was launched or contacted.");
          return;
        }
        setProbeResult(payload);
        const checkedAt = payload.checked_at;
        if (payload.executed && checkedAt && !payload.stale_configuration) {
          setSettings((current) => current ? {
            ...current,
            probe_statuses: {
              ...current.probe_statuses,
              [selected.name]: {
                status: payload.status,
                checked_at: checkedAt,
                latency_ms: payload.latency_ms,
                discovered_count: payload.discovered_tools.length,
              },
            },
          } : current);
        }
      });
    } catch (caughtError) {
      if (caughtError instanceof DOMException && caughtError.name === "AbortError") return;
      probeGuard.commit(ticket, () => {
        setProbeIntent(null);
        restoreProbeFocus();
        setError(caughtError instanceof Error ? caughtError.message : "Connection check could not finish.");
      });
    } finally {
      probeGuard.commit(ticket, () => {
        if (probeAbortRef.current === abortController) probeAbortRef.current = null;
        setProbeStage(null);
      });
    }
  }

  function reconcileTool(toolName: string, include: boolean) {
    setTools((current) => reconcileMCPToolDraft(current, toolName, include));
    setNotice(
      include
        ? `${toolName} added to the draft allowlist. Save the server to apply it.`
        : `${toolName} removed from the draft allowlist. Save the server to apply it.`,
    );
  }

  const validName = /^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$/.test(draft.name);
  const canSave = validName && Boolean(draft.display_name.trim()) && (!draft.enabled || Boolean(parseList(tools).length));

  return (
    <section
      className="auth-panel mcp-settings-panel"
      id="settings-step-mcp"
      aria-labelledby="mcp-settings-title"
    >
      <div className="auth-panel-heading mcp-settings-heading">
        <div>
          <span className="eyebrow">MCP connections</span>
          <h2 id="mcp-settings-title" tabIndex={-1}>Choose the outside tools Pilot can see</h2>
          <p>Every server is private configuration with an explicit tool allowlist. Stdio servers run commands on the CareerPilot runtime host.</p>
        </div>
        <span className={enabledCount ? "mcp-count is-ready" : "mcp-count"}>{enabledCount} enabled</span>
      </div>

      {settings ? (
        <div className="mcp-settings-layout">
          <aside className="mcp-settings-list" aria-label="Configured MCP servers">
            <button className="agent-resource-new" type="button" onClick={() => loadDraft(emptyServer(), true)}>
              <span aria-hidden="true">+</span> Add server
            </button>
            {settings.servers.map((server) => (
              <button
                className={server.name === selectedName && !creating ? "is-selected" : ""}
                key={server.name}
                onClick={() => loadDraft(server)}
                type="button"
              >
                <span className={server.enabled ? "mcp-server-dot is-enabled" : "mcp-server-dot"} aria-hidden="true" />
                <span>
                  <strong>{server.display_name}</strong>
                  <small>{server.transport.toUpperCase()} · {server.enabled ? "Enabled" : "Off"}</small>
                </span>
              </button>
            ))}
          </aside>

          <div className="mcp-settings-editor">
            <div className="mcp-editor-title">
              <div>
                <span>{draft.preset ? "Curated preset" : creating ? "New connection" : "Custom connection"}</span>
                <strong>{draft.display_name}</strong>
              </div>
              {!creating && selected ? (
                <div className="mcp-editor-controls">
                  {lastProbe ? (
                    <span className={`mcp-probe-badge is-${lastProbe.status}`} title={`Checked ${formattedProbeTime(lastProbe.checked_at)}`}>
                      {PROBE_STATUS_LABELS[lastProbe.status]}
                    </span>
                  ) : null}
                  <button
                    className="secondary-action mcp-probe-start"
                    disabled={saving || probeStage !== null || !probeTargetIsSaved}
                    onClick={() => void beginProbe()}
                    ref={probeTriggerRef}
                    title={probeTargetIsSaved ? "Review and run one bounded connection check" : "Save connection changes before checking"}
                    type="button"
                  >
                    {probeStage === "intent" ? "Preparing review…" : probeStage === "probe" ? "Checking…" : "Test saved connection"}
                  </button>
                  <button className="text-action danger-action" disabled={saving || probeStage !== null} onClick={() => void remove()} type="button">Remove</button>
                </div>
              ) : null}
            </div>

            {draft.warning ? <div className="mcp-risk-note"><strong>Before enabling</strong><p>{draft.warning}</p></div> : null}
            {draft.name === "linkedin-search" ? (
              <div className="linkedin-mcp-note">
                <span>LinkedIn search</span>
                <p>Uses <code>uvx mcp-server-linkedin@latest</code>. The first search may open a browser so you can sign in; the session stays on the CareerPilot runtime host.</p>
                {draft.source_url ? <a href={draft.source_url} rel="noreferrer" target="_blank">Review the community server <span aria-hidden="true">↗</span></a> : null}
                {draft.command_available === false ? <small><strong>uvx was not found.</strong> Install uv before enabling this server.</small> : null}
              </div>
            ) : null}

            {probeResult ? (
              <section
                aria-atomic="true"
                aria-live="polite"
                className={`mcp-probe-result is-${probeResult.status}`}
              >
                <div className="mcp-probe-result-heading">
                  <div>
                    <span>Saved connection check</span>
                    <strong>{PROBE_STATUS_LABELS[probeResult.status]}</strong>
                  </div>
                  <small>{probeResult.latency_ms} ms{probeResult.truncated ? " · bounded result truncated" : ""}</small>
                </div>
                <p>{probeResult.message}</p>
                <div className="mcp-probe-groups">
                  <section aria-labelledby="mcp-allowed-present-title">
                    <h4 id="mcp-allowed-present-title">Allowed and present <span>{allowedPresentTools.length}</span></h4>
                    {allowedPresentTools.length ? (
                      <ul className="mcp-discovered-tools">
                        {allowedPresentTools.map((tool, index) => (
                          <li key={`${tool.name}-${index}`}>
                            <div>
                              <code>{tool.name}</code>
                              <span className="mcp-untrusted-label">Untrusted server-reported description</span>
                              <p>{tool.description || "No description reported."}</p>
                            </div>
                            <button className="secondary-action" onClick={() => reconcileTool(tool.name, false)} type="button">Remove from draft</button>
                          </li>
                        ))}
                      </ul>
                    ) : <p className="mcp-probe-empty-group">No allowed tools were reported.</p>}
                  </section>

                  <section aria-labelledby="mcp-new-tools-title">
                    <h4 id="mcp-new-tools-title">New tools available <span>{newDiscoveredTools.length}</span></h4>
                    {newDiscoveredTools.length ? (
                      <ul className="mcp-discovered-tools">
                        {newDiscoveredTools.map((tool, index) => {
                          const included = draftToolNames.has(tool.name);
                          return (
                            <li key={`${tool.name}-${index}`}>
                              <div>
                                <code>{tool.name}</code>
                                <span className="mcp-untrusted-label">Untrusted server-reported description</span>
                                <p>{tool.description || "No description reported."}</p>
                                {tool.policy_reason ? <small>{tool.policy_reason}</small> : null}
                              </div>
                              <button
                                className="secondary-action"
                                disabled={!tool.selectable}
                                onClick={() => reconcileTool(tool.name, !included)}
                                type="button"
                              >
                                {included ? "Remove from draft" : "Add to draft"}
                              </button>
                            </li>
                          );
                        })}
                      </ul>
                    ) : <p className="mcp-probe-empty-group">No new tools were reported.</p>}
                  </section>

                  <section aria-labelledby="mcp-allowed-missing-title">
                    <h4 id="mcp-allowed-missing-title">Allowed but missing <span>{probeResult.allowed_missing.length}</span></h4>
                    {probeResult.allowed_missing.length ? (
                      <ul className="mcp-probe-missing">
                        {probeResult.allowed_missing.map((toolName) => {
                          const blockedClaim = probeResult.discovered_tools.find((tool) => tool.name === toolName && !tool.selectable);
                          return (
                            <li key={toolName}>
                              <code>{toolName}</code>
                              <span>{blockedClaim?.policy_reason ?? "The server did not report this exact allowed name."}</span>
                            </li>
                          );
                        })}
                      </ul>
                    ) : <p className="mcp-probe-empty-group">Every allowed tool was reported.</p>}
                  </section>
                </div>
                {probeResult.discovered_tools.length ? (
                  <small className="mcp-probe-draft-note">Discovery never changes the saved allowlist. Review the draft, then choose Save server.</small>
                ) : null}
              </section>
            ) : null}

            <div className="mcp-form-grid">
              <label>Name<input disabled={!creating || saving} maxLength={80} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "") }))} spellCheck={false} value={draft.name} /></label>
              <label>Display name<input disabled={saving} maxLength={120} onChange={(event) => setDraft((current) => ({ ...current, display_name: event.target.value }))} value={draft.display_name} /></label>
              <label className="mcp-form-wide">Description<textarea disabled={saving} maxLength={1000} onChange={(event) => setDraft((current) => ({ ...current, description: event.target.value }))} value={draft.description} /></label>
              <label>Transport<select disabled={saving} onChange={(event) => { invalidateProbe(); setDraft((current) => ({ ...current, transport: event.target.value as "stdio" | "http" })); }} value={draft.transport}><option value="stdio">Local command (stdio)</option><option value="http">HTTP endpoint</option></select></label>
              <label className="mcp-toggle-label"><input checked={draft.enabled} disabled={saving} onChange={(event) => setDraft((current) => ({ ...current, enabled: event.target.checked }))} type="checkbox" /><span><strong>Enabled</strong><small>Pilot can launch and use this server.</small></span></label>
              {draft.transport === "stdio" ? (
                <>
                  <label>Command<input disabled={saving} onChange={(event) => { invalidateProbe(); setDraft((current) => ({ ...current, command: event.target.value })); }} placeholder="uvx" spellCheck={false} value={draft.command ?? ""} /></label>
                  <label>Arguments<textarea disabled={saving} onChange={(event) => { invalidateProbe(); setArgs(event.target.value); }} placeholder="One argument per line" spellCheck={false} value={args} /></label>
                  <label className="mcp-form-wide">Non-secret environment values<textarea disabled={saving} onChange={(event) => { invalidateProbe(); setEnvironment(event.target.value); }} placeholder="NAME=value" spellCheck={false} value={environment} /></label>
                </>
              ) : (
                <label className="mcp-form-wide">HTTPS endpoint<input disabled={saving} onChange={(event) => { invalidateProbe(); setDraft((current) => ({ ...current, url: event.target.value })); }} placeholder="https://mcp.example.com/mcp" spellCheck={false} value={draft.url ?? ""} /></label>
              )}
              <label className="mcp-form-wide">Allowed tools<textarea disabled={saving} onChange={(event) => setTools(event.target.value)} placeholder="One exact tool name per line" spellCheck={false} value={tools} /><small>Required before a server can be enabled.</small></label>
              <label className="mcp-form-wide">Forward existing environment variables<textarea disabled={saving} onChange={(event) => { invalidateProbe(); setForwardedEnvironment(event.target.value); }} placeholder="VARIABLE_NAME (one per line)" spellCheck={false} value={forwardedEnvironment} /><small>Values are never returned to the browser or stored in this form.</small></label>
            </div>
            <div className="mcp-editor-actions">
              <p>Only add servers you trust. A local command has the same device access as the CareerPilot process.</p>
              <button className="auth-action" disabled={saving || !canSave} onClick={() => void save()} type="button">{saving ? "Saving…" : creating ? "Add server" : "Save server"}</button>
            </div>
          </div>
        </div>
      ) : loadState.status === "loading" ? (
        <p className="settings-loading" role="status">Loading MCP settings…</p>
      ) : null}

      {notice ? <div className="capability-notice" role="status">{notice}</div> : null}
      {loadState.error ? (
        <div className="settings-recovery-error" role="alert">
          <div>
            <strong>MCP settings unavailable</strong>
            <span>{loadState.error} Your selected server and unsaved configuration are preserved.</span>
          </div>
          <button
            className="secondary-action"
            disabled={loadState.status === "loading"}
            onClick={() => setLoadRetry(nextReadGeneration)}
            type="button"
          >
            {loadState.status === "loading" ? "Retrying…" : "Retry MCP"}
          </button>
        </div>
      ) : null}
      {error ? <div className="auth-error" role="alert"><strong>MCP settings</strong><span>{error}</span></div> : null}
      {probeIntent ? (
        <div className="mcp-probe-modal-backdrop">
          <div
            aria-describedby="mcp-probe-disclosure-risk"
            aria-labelledby="mcp-probe-disclosure-title"
            aria-modal="true"
            className="mcp-probe-modal"
            onKeyDown={(event) => {
              if (event.key === "Escape" && probeStage !== "probe") {
                event.preventDefault();
                void decideProbe("denied");
              }
              if (event.key === "Tab") {
                const controls = Array.from(
                  event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"),
                );
                const first = controls[0];
                const last = controls.at(-1);
                if (!first || !last) return;
                if (event.shiftKey && document.activeElement === first) {
                  event.preventDefault();
                  last.focus();
                } else if (!event.shiftKey && document.activeElement === last) {
                  event.preventDefault();
                  first.focus();
                }
              }
            }}
            role="dialog"
          >
            <span className="eyebrow">Manual one-time check</span>
            <h3 id="mcp-probe-disclosure-title">Review what CareerPilot will do</h3>
            <p id="mcp-probe-disclosure-risk">{probeIntent.disclosure.risk}</p>
            <dl className="mcp-probe-disclosure">
              <div>
                <dt>{probeIntent.disclosure.target_label}</dt>
                <dd>
                  <code>{probeIntent.disclosure.target}</code>
                  <small>{probeIntent.disclosure.bound_target_note}</small>
                </dd>
              </div>
              <div><dt>Time limit</dt><dd>{probeIntent.disclosure.timeout_seconds} seconds</dd></div>
              <div><dt>Configuration</dt><dd>Not changed by this check</dd></div>
            </dl>
            <div className="mcp-probe-operations">
              <strong>Only these MCP methods and bounded request counts</strong>
              <ol>{probeIntent.disclosure.operations.map((operation) => <li key={operation}>{operation}</li>)}</ol>
              <small>No tool calls, prompts, resources, OAuth flow, retries, or background checks.</small>
            </div>
            <div aria-live="polite" className="mcp-probe-modal-actions">
              <button autoFocus className="secondary-action" disabled={probeStage === "probe"} onClick={() => void decideProbe("denied")} type="button">Cancel</button>
              <button className="auth-action" disabled={probeStage === "probe"} onClick={() => void decideProbe("approved")} type="button">
                {probeStage === "probe" ? "Checking saved connection…" : "Run this check once"}
              </button>
            </div>
            <small className="mcp-probe-expiry">This review expires {formattedProbeTime(probeIntent.expires_at)}.</small>
          </div>
        </div>
      ) : null}
    </section>
  );
}
