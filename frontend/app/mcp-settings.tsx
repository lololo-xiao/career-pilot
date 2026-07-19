"use client";

import { useEffect, useMemo, useReducer, useState } from "react";

import {
  createSettingsDraftHydration,
  createSettingsReadController,
  initialRecoverableReadState,
  isMCPSettingsResponse,
  nextReadGeneration,
  recoverableReadReducer,
  startRecoverableSettingsRead,
} from "./settings-recovery";
import type { MCPServerSettings, MCPSettingsResponse } from "./types";


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

  const selected = useMemo(
    () => settings?.servers.find((server) => server.name === selectedName) ?? null,
    [selectedName, settings],
  );
  const enabledCount = settings?.servers.filter((server) => server.enabled).length ?? 0;

  function loadDraft(server: MCPServerSettings, isCreating = false) {
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
    };
  }, [apiBaseUrl, draftHydration, loadController, loadRetry]);

  async function save() {
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
          <p>Every server is local configuration with an explicit tool allowlist. Stdio servers run commands on this device.</p>
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
              {!creating && selected ? <button className="text-action danger-action" disabled={saving} onClick={() => void remove()} type="button">Remove</button> : null}
            </div>

            {draft.warning ? <div className="mcp-risk-note"><strong>Before enabling</strong><p>{draft.warning}</p></div> : null}
            {draft.name === "linkedin-search" ? (
              <div className="linkedin-mcp-note">
                <span>LinkedIn search</span>
                <p>Uses <code>uvx mcp-server-linkedin@latest</code>. The first search may open a browser so you can sign in; the session stays on this device.</p>
                {draft.source_url ? <a href={draft.source_url} rel="noreferrer" target="_blank">Review the community server <span aria-hidden="true">↗</span></a> : null}
                {draft.command_available === false ? <small><strong>uvx was not found.</strong> Install uv before enabling this server.</small> : null}
              </div>
            ) : null}

            <div className="mcp-form-grid">
              <label>Name<input disabled={!creating || saving} maxLength={80} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "") }))} spellCheck={false} value={draft.name} /></label>
              <label>Display name<input disabled={saving} maxLength={120} onChange={(event) => setDraft((current) => ({ ...current, display_name: event.target.value }))} value={draft.display_name} /></label>
              <label className="mcp-form-wide">Description<textarea disabled={saving} maxLength={1000} onChange={(event) => setDraft((current) => ({ ...current, description: event.target.value }))} value={draft.description} /></label>
              <label>Transport<select disabled={saving} onChange={(event) => setDraft((current) => ({ ...current, transport: event.target.value as "stdio" | "http" }))} value={draft.transport}><option value="stdio">Local command (stdio)</option><option value="http">HTTP endpoint</option></select></label>
              <label className="mcp-toggle-label"><input checked={draft.enabled} disabled={saving} onChange={(event) => setDraft((current) => ({ ...current, enabled: event.target.checked }))} type="checkbox" /><span><strong>Enabled</strong><small>Pilot can launch and use this server.</small></span></label>
              {draft.transport === "stdio" ? (
                <>
                  <label>Command<input disabled={saving} onChange={(event) => setDraft((current) => ({ ...current, command: event.target.value }))} placeholder="uvx" spellCheck={false} value={draft.command ?? ""} /></label>
                  <label>Arguments<textarea disabled={saving} onChange={(event) => setArgs(event.target.value)} placeholder="One argument per line" spellCheck={false} value={args} /></label>
                  <label className="mcp-form-wide">Non-secret environment values<textarea disabled={saving} onChange={(event) => setEnvironment(event.target.value)} placeholder="NAME=value" spellCheck={false} value={environment} /></label>
                </>
              ) : (
                <label className="mcp-form-wide">HTTPS endpoint<input disabled={saving} onChange={(event) => setDraft((current) => ({ ...current, url: event.target.value }))} placeholder="https://mcp.example.com/mcp" spellCheck={false} value={draft.url ?? ""} /></label>
              )}
              <label className="mcp-form-wide">Allowed tools<textarea disabled={saving} onChange={(event) => setTools(event.target.value)} placeholder="One exact tool name per line" spellCheck={false} value={tools} /><small>Required before a server can be enabled.</small></label>
              <label className="mcp-form-wide">Forward existing environment variables<textarea disabled={saving} onChange={(event) => setForwardedEnvironment(event.target.value)} placeholder="VARIABLE_NAME (one per line)" spellCheck={false} value={forwardedEnvironment} /><small>Values are never returned to the browser or stored in this form.</small></label>
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
    </section>
  );
}
