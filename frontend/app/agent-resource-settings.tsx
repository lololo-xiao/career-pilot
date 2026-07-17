"use client";

import { useEffect, useMemo, useState } from "react";

import type {
  AgentAsset,
  AgentAssetKind,
  AgentAssetSettingsResponse,
} from "./types";


interface AgentResourceSettingsProps {
  apiBaseUrl: string;
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

function skillTemplate(name: string): string {
  return `---\nname: ${name}\ndescription: Describe when Pilot should use this skill.\n---\n\n# ${name.replaceAll("-", " ")}\n\n- Add clear, safe instructions here.\n`;
}

function memoryTemplate(name: string): string {
  return `# ${name.replaceAll("-", " ")}\n\nAdd a durable preference, correction, commitment, or workflow note here.\n`;
}

export function AgentResourceSettings({ apiBaseUrl }: AgentResourceSettingsProps) {
  const [settings, setSettings] = useState<AgentAssetSettingsResponse | null>(null);
  const [kind, setKind] = useState<AgentAssetKind>("memory");
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [draftName, setDraftName] = useState("");
  const [draftContent, setDraftContent] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const currentAssets = useMemo(
    () => (kind === "memory" ? settings?.memories ?? [] : settings?.skills ?? []),
    [kind, settings],
  );
  const selected = useMemo(
    () => currentAssets.find((asset) => asset.name === selectedName) ?? null,
    [currentAssets, selectedName],
  );

  function selectAsset(asset: AgentAsset) {
    setCreating(false);
    setSelectedName(asset.name);
    setDraftName(asset.name);
    setDraftContent(asset.content);
    setError(null);
    setNotice(null);
  }

  function startNew(nextKind: AgentAssetKind = kind) {
    const name = nextKind === "memory" ? "new-memory" : "new-skill";
    setKind(nextKind);
    setCreating(true);
    setSelectedName(null);
    setDraftName(name);
    setDraftContent(nextKind === "memory" ? memoryTemplate(name) : skillTemplate(name));
    setError(null);
    setNotice(null);
  }

  function applySettings(payload: AgentAssetSettingsResponse, preferredName?: string) {
    setSettings(payload);
    const list = kind === "memory" ? payload.memories : payload.skills;
    const next = list.find((asset) => asset.name === preferredName) ?? list[0] ?? null;
    if (next) selectAsset(next);
    else startNew(kind);
  }

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const response = await fetch(`${apiBaseUrl}/settings/agent-resources`, {
          credentials: "include",
        });
        if (!response.ok) {
          throw new Error(await readError(response, "Agent files could not load."));
        }
        const payload = (await response.json()) as AgentAssetSettingsResponse;
        if (!active) return;
        setSettings(payload);
        const first = payload.memories[0] ?? null;
        if (first) {
          setCreating(false);
          setSelectedName(first.name);
          setDraftName(first.name);
          setDraftContent(first.content);
        } else {
          const name = "new-memory";
          setKind("memory");
          setCreating(true);
          setSelectedName(null);
          setDraftName(name);
          setDraftContent(memoryTemplate(name));
        }
      } catch (caughtError) {
        if (active) {
          setError(caughtError instanceof Error ? caughtError.message : "Agent files could not load.");
        }
      }
    }
    void load();
    return () => {
      active = false;
    };
  }, [apiBaseUrl]);

  function switchKind(nextKind: AgentAssetKind) {
    setKind(nextKind);
    const list = nextKind === "memory" ? settings?.memories ?? [] : settings?.skills ?? [];
    if (list[0]) selectAsset(list[0]);
    else startNew(nextKind);
  }

  function changeName(nextName: string) {
    const normalized = nextName.toLowerCase().replace(/[^a-z0-9-]/g, "");
    if (creating && kind === "skill") {
      setDraftContent((current) => current.replace(`name: ${draftName}`, `name: ${normalized}`));
    }
    setDraftName(normalized);
  }

  async function save() {
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const endpoint = creating
        ? `${apiBaseUrl}/settings/agent-resources/${kind}`
        : `${apiBaseUrl}/settings/agent-resources/${kind}/${encodeURIComponent(selectedName ?? "")}`;
      const response = await fetch(endpoint, {
        method: creating ? "POST" : "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: draftName, content: draftContent }),
      });
      if (!response.ok) {
        throw new Error(await readError(response, "Agent file could not be saved."));
      }
      const payload = (await response.json()) as AgentAssetSettingsResponse;
      applySettings(payload, draftName);
      setNotice(`${kind === "memory" ? "Memory" : "Skill"} saved. Pilot will use it on the next turn.`);
    } catch (caughtError) {
      setError(caughtError instanceof Error ? caughtError.message : "Agent file could not be saved.");
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!selected || !selected.editable) return;
    if (!window.confirm(`Remove ${selected.title}? This cannot be undone.`)) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const response = await fetch(
        `${apiBaseUrl}/settings/agent-resources/${kind}/${encodeURIComponent(selected.name)}`,
        { method: "DELETE", credentials: "include" },
      );
      if (!response.ok) {
        throw new Error(await readError(response, "Agent file could not be removed."));
      }
      const payload = (await response.json()) as AgentAssetSettingsResponse;
      applySettings(payload);
      setNotice(`${kind === "memory" ? "Memory" : "Skill"} removed.`);
    } catch (caughtError) {
      setError(caughtError instanceof Error ? caughtError.message : "Agent file could not be removed.");
    } finally {
      setSaving(false);
    }
  }

  const readOnly = Boolean(selected?.built_in && !creating);
  const validName = /^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$/.test(draftName);

  return (
    <section className="auth-panel agent-resource-panel" aria-labelledby="agent-resource-title">
      <div className="auth-panel-heading">
        <span className="eyebrow">Agent workspace</span>
        <h2 id="agent-resource-title">Memories and skills, in plain sight</h2>
        <p>
          Review exactly what Pilot carries forward. Your files are editable; product-owned
          skills stay visible and read-only.
        </p>
      </div>

      <div className="agent-resource-tabs" role="tablist" aria-label="Agent file type">
        <button className={kind === "memory" ? "is-active" : ""} onClick={() => switchKind("memory")} role="tab" type="button">
          Memories <span>{settings?.memories.length ?? 0}</span>
        </button>
        <button className={kind === "skill" ? "is-active" : ""} onClick={() => switchKind("skill")} role="tab" type="button">
          Skills <span>{settings?.skills.length ?? 0}</span>
        </button>
      </div>

      {settings ? (
        <div className="agent-resource-layout">
          <aside className="agent-resource-list" aria-label={`${kind} files`}>
            <button className="agent-resource-new" type="button" onClick={() => startNew()}>
              <span aria-hidden="true">+</span> New {kind}
            </button>
            {currentAssets.map((asset) => (
              <button
                className={asset.name === selectedName && !creating ? "is-selected" : ""}
                key={`${asset.kind}:${asset.name}`}
                onClick={() => selectAsset(asset)}
                type="button"
              >
                <strong>{asset.title}</strong>
                <small>{asset.built_in ? "Built in" : asset.path}</small>
              </button>
            ))}
            {currentAssets.length === 0 ? <p>No {kind === "memory" ? "memories" : "skills"} yet.</p> : null}
          </aside>

          <div className="agent-resource-editor">
            <div className="agent-resource-editor-heading">
              <div>
                <span>{readOnly ? "Built-in · read only" : creating ? `New ${kind}` : "Your file"}</span>
                <strong>{readOnly ? selected?.path : `${kind === "memory" ? "memories" : "skills"}/${draftName}`}</strong>
              </div>
              {selected?.editable && !creating ? (
                <button className="text-action danger-action" disabled={saving} onClick={() => void remove()} type="button">Remove</button>
              ) : null}
            </div>
            <label htmlFor={`agent-resource-name-${kind}`}>File name</label>
            <input
              id={`agent-resource-name-${kind}`}
              disabled={readOnly || (!creating && Boolean(selected)) || saving}
              maxLength={80}
              onChange={(event) => changeName(event.target.value)}
              spellCheck={false}
              value={draftName}
            />
            <label htmlFor={`agent-resource-content-${kind}`}>Markdown content</label>
            <textarea
              id={`agent-resource-content-${kind}`}
              className="agent-resource-content"
              disabled={saving}
              onChange={(event) => setDraftContent(event.target.value)}
              readOnly={readOnly}
              spellCheck={false}
              value={draftContent}
            />
            {!readOnly ? (
              <div className="agent-resource-actions">
                <p>Saved locally. Changes restart Pilot’s local runtime without exposing these files to the browser again.</p>
                <button className="auth-action" disabled={saving || !validName || !draftContent.trim()} onClick={() => void save()} type="button">
                  {saving ? "Saving…" : creating ? `Create ${kind}` : "Save changes"}
                </button>
              </div>
            ) : (
              <p className="agent-resource-readonly-note">Built-in skills are supplied by CareerPilot and cannot be changed from this workspace.</p>
            )}
          </div>
        </div>
      ) : !error ? <p className="settings-loading">Loading agent files…</p> : null}

      {notice ? <div className="capability-notice" role="status">{notice}</div> : null}
      {error ? <div className="auth-error" role="alert"><strong>Agent workspace</strong><span>{error}</span></div> : null}
    </section>
  );
}
