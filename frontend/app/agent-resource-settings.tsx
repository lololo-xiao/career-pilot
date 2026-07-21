"use client";

import { useEffect, useMemo, useReducer, useRef, useState } from "react";

import {
  agentAssetNameMaxLength,
  buildUserSkillExport,
  createSkillInstallRequest,
  createTemplatePreview,
  CURATED_SKILL_TEMPLATES,
  getSkillInstallReadiness,
  isValidAgentAssetDraftName,
  isCurrentSkillPreviewGeneration,
  nextSkillPreviewGeneration,
  readSkillImportFile,
  renameSkillPreview,
  type SkillPreviewDraft,
} from "./skill-library";
import {
  createSettingsDraftHydration,
  createSettingsReadController,
  initialRecoverableReadState,
  isAgentAssetSettingsResponse,
  nextReadGeneration,
  recoverableReadReducer,
  startRecoverableSettingsRead,
} from "./settings-recovery";
import { shareNativeBlob } from "./native-platform";
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
  const [loadRetry, setLoadRetry] = useState(0);
  const [loadController] = useState(
    () => createSettingsReadController("resources"),
  );
  const [draftHydration] = useState(createSettingsDraftHydration);
  const [loadState, dispatchLoad] = useReducer(
    recoverableReadReducer<AgentAssetSettingsResponse>,
    undefined,
    () => initialRecoverableReadState<AgentAssetSettingsResponse>(),
  );
  const [kind, setKind] = useState<AgentAssetKind>("memory");
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [draftName, setDraftName] = useState("");
  const [draftContent, setDraftContent] = useState("");
  const [skillPreview, setSkillPreview] = useState<SkillPreviewDraft | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const skillPreviewGeneration = useRef(0);

  const currentAssets = useMemo(
    () => (kind === "memory" ? settings?.memories ?? [] : settings?.skills ?? []),
    [kind, settings],
  );
  const selected = useMemo(
    () => currentAssets.find((asset) => asset.name === selectedName) ?? null,
    [currentAssets, selectedName],
  );
  const existingSkillNames = useMemo(
    () => settings?.skills.map((skill) => skill.name) ?? [],
    [settings],
  );
  const skillInstallReadiness = useMemo(
    () => skillPreview
      ? getSkillInstallReadiness(skillPreview, existingSkillNames)
      : null,
    [existingSkillNames, skillPreview],
  );

  function selectAsset(asset: AgentAsset) {
    skillPreviewGeneration.current = nextSkillPreviewGeneration(skillPreviewGeneration.current);
    setCreating(false);
    setSkillPreview(null);
    setSelectedName(asset.name);
    setDraftName(asset.name);
    setDraftContent(asset.content);
    setError(null);
    setNotice(null);
  }

  function startNew(nextKind: AgentAssetKind = kind) {
    skillPreviewGeneration.current = nextSkillPreviewGeneration(skillPreviewGeneration.current);
    const name = nextKind === "memory" ? "new-memory" : "new-skill";
    setKind(nextKind);
    setCreating(true);
    setSkillPreview(null);
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
    const abortController = new AbortController();
    const read = startRecoverableSettingsRead({
      apiBaseUrl,
      controller: loadController,
      fallback: "Agent files could not load.",
      onFailure(error, generation) {
        dispatchLoad({ type: "failure", generation, error });
      },
      onStart(generation) {
        dispatchLoad({ type: "start", generation });
      },
      onSuccess(payload, generation) {
        setSettings(payload);
        draftHydration.hydrate(() => {
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
        });
        dispatchLoad({ type: "success", generation, data: payload });
      },
      signal: abortController.signal,
      validate: isAgentAssetSettingsResponse,
    });
    void read.completion;
    return () => {
      abortController.abort();
      loadController.invalidate(read.ticket);
    };
  }, [apiBaseUrl, draftHydration, loadController, loadRetry]);

  function switchKind(nextKind: AgentAssetKind) {
    setKind(nextKind);
    const list = nextKind === "memory" ? settings?.memories ?? [] : settings?.skills ?? [];
    if (list[0]) selectAsset(list[0]);
    else startNew(nextKind);
  }

  function changeName(nextName: string) {
    if (creating && kind === "skill" && skillPreview) {
      const renamed = renameSkillPreview(skillPreview, nextName);
      setSkillPreview(renamed);
      setDraftName(renamed.name);
      setDraftContent(renamed.content);
      setError(null);
      setNotice(null);
      return;
    }
    const normalized = nextName.toLowerCase().replace(/[^a-z0-9-]/g, "");
    if (creating && kind === "skill") {
      setDraftContent((current) => current.replace(`name: ${draftName}`, `name: ${normalized}`));
    }
    setDraftName(normalized);
  }

  function showSkillPreview(preview: SkillPreviewDraft) {
    skillPreviewGeneration.current = nextSkillPreviewGeneration(skillPreviewGeneration.current);
    setKind("skill");
    setCreating(true);
    setSelectedName(null);
    setSkillPreview(preview);
    setDraftName(preview.name);
    setDraftContent(preview.content);
    setError(null);
    setNotice("Skill preview loaded. Nothing has been installed or activated.");
  }

  async function previewImportedSkill(file: File | undefined) {
    if (!file) return;
    const generation = nextSkillPreviewGeneration(skillPreviewGeneration.current);
    skillPreviewGeneration.current = generation;
    try {
      const preview = await readSkillImportFile(file);
      if (!isCurrentSkillPreviewGeneration(generation, skillPreviewGeneration.current)) return;
      showSkillPreview(preview);
    } catch (caughtError) {
      if (!isCurrentSkillPreviewGeneration(generation, skillPreviewGeneration.current)) return;
      setError(caughtError instanceof Error ? caughtError.message : "The skill file could not be imported.");
      setNotice(null);
    }
  }

  async function save() {
    let requestBody = { name: draftName, content: draftContent };
    const installingPreview = Boolean(skillPreview);
    if (skillPreview) {
      try {
        requestBody = createSkillInstallRequest(skillPreview, existingSkillNames);
      } catch (caughtError) {
        setError(caughtError instanceof Error ? caughtError.message : "The skill preview cannot be installed.");
        setNotice(null);
        return;
      }
    }
    skillPreviewGeneration.current = nextSkillPreviewGeneration(skillPreviewGeneration.current);
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
        body: JSON.stringify(requestBody),
      });
      if (!response.ok) {
        const detail = await readError(response, "Agent file could not be saved.");
        throw new Error(
          installingPreview && response.status === 409
            ? `Skill name conflict: ${detail} Choose another name; your preview is preserved.`
            : detail,
        );
      }
      const payload = (await response.json()) as AgentAssetSettingsResponse;
      applySettings(payload, requestBody.name);
      setNotice(
        installingPreview
          ? "Skill installed. Pilot will use it on the next turn."
          : `${kind === "memory" ? "Memory" : "Skill"} saved. Pilot will use it on the next turn.`,
      );
    } catch (caughtError) {
      setError(caughtError instanceof Error ? caughtError.message : "Agent file could not be saved.");
    } finally {
      setSaving(false);
    }
  }

  async function exportSelectedSkill() {
    if (!selected) return;
    try {
      const exported = buildUserSkillExport(selected);
      const blob = new Blob([exported.content], { type: exported.mimeType });
      if (await shareNativeBlob(blob, exported.filename, "CareerPilot skill")) {
        setError(null);
        setNotice(`Shared ${exported.filename} with the exact saved skill content.`);
        return;
      }
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = exported.filename;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      setError(null);
      setNotice(`Exported ${exported.filename} with the exact saved skill content.`);
    } catch (caughtError) {
      setError(caughtError instanceof Error ? caughtError.message : "The skill could not be exported.");
      setNotice(null);
    }
  }

  async function remove() {
    if (!selected || !selected.editable) return;
    if (!window.confirm(`Remove ${selected.title}? This cannot be undone.`)) return;
    skillPreviewGeneration.current = nextSkillPreviewGeneration(skillPreviewGeneration.current);
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
  const validName = isValidAgentAssetDraftName(kind, draftName, creating);

  return (
    <section
      className="auth-panel agent-resource-panel"
      id="settings-step-resources"
      aria-labelledby="agent-resource-title"
    >
      <div className="auth-panel-heading">
        <span className="eyebrow">Agent workspace</span>
        <h2 id="agent-resource-title" tabIndex={-1}>Memories and skills, in plain sight</h2>
        <p>
          Review exactly what Pilot carries forward. Your files are editable; product-owned
          skills stay visible and read-only.
        </p>
      </div>

      <div className="agent-resource-tabs" role="tablist" aria-label="Agent file type">
        <button aria-selected={kind === "memory"} className={kind === "memory" ? "is-active" : ""} disabled={saving || !settings} onClick={() => switchKind("memory")} role="tab" type="button">
          Memories <span>{settings?.memories.length ?? 0}</span>
        </button>
        <button aria-selected={kind === "skill"} className={kind === "skill" ? "is-active" : ""} disabled={saving || !settings} onClick={() => switchKind("skill")} role="tab" type="button">
          Skills <span>{settings?.skills.length ?? 0}</span>
        </button>
      </div>

      {settings ? (
        <div className="agent-resource-layout">
          <aside className="agent-resource-list" aria-label={`${kind} files`}>
            {kind === "skill" ? (
              <section className="skill-library" aria-labelledby="skill-library-title">
                <div className="skill-library-heading">
                  <span>Preview first</span>
                  <h3 id="skill-library-title">Starter library</h3>
                  <p>Selecting or importing never installs a skill.</p>
                </div>
                <div className="skill-library-options">
                  {CURATED_SKILL_TEMPLATES.map((template) => (
                    <button
                      aria-pressed={skillPreview?.source === "curated-template" && skillPreview.templateName === template.name}
                      className={skillPreview?.source === "curated-template" && skillPreview.templateName === template.name ? "is-selected" : ""}
                      disabled={saving}
                      key={template.name}
                      onClick={() => showSkillPreview(createTemplatePreview(template))}
                      type="button"
                    >
                      <strong>{template.title}</strong>
                      <small>{template.name}</small>
                    </button>
                  ))}
                </div>
                <label className="skill-import-control" htmlFor="skill-library-import">
                  Import one SKILL.md
                </label>
                <input
                  accept=".md,text/markdown,text/plain"
                  id="skill-library-import"
                  disabled={saving}
                  onChange={(event) => {
                    const input = event.currentTarget;
                    const file = input.files?.[0];
                    input.value = "";
                    void previewImportedSkill(file);
                  }}
                  type="file"
                />
                <small className="skill-import-limit">Regular UTF-8 file · 64 KiB · skill names up to 64 characters</small>
              </section>
            ) : null}
            <button className="agent-resource-new" disabled={saving} type="button" onClick={() => startNew()}>
              <span aria-hidden="true">+</span> New {kind}
            </button>
            {currentAssets.map((asset) => (
              <button
                className={asset.name === selectedName && !creating ? "is-selected" : ""}
                disabled={saving}
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
                <span>{readOnly ? "Built-in · read only" : skillPreview ? "Preview only · not installed" : creating ? `New ${kind}` : "Your file"}</span>
                <strong>{skillPreview ? skillPreview.title : readOnly ? selected?.path : `${kind === "memory" ? "memories" : "skills"}/${draftName}`}</strong>
              </div>
              <div className="agent-resource-heading-actions">
                {kind === "skill" && selected?.editable && !creating ? (
                  <button className="text-action" disabled={saving} onClick={exportSelectedSkill} type="button">Export skill</button>
                ) : null}
                {selected?.editable && !creating ? (
                  <button className="text-action danger-action" disabled={saving} onClick={() => void remove()} type="button">Remove</button>
                ) : null}
              </div>
            </div>
            {skillPreview ? (
              <dl className="skill-preview-metadata" aria-label="Complete skill preview metadata">
                <div><dt>Frontmatter name</dt><dd>{draftName || "Choose a name"}</dd></div>
                <div><dt>Description</dt><dd>{skillPreview.description}</dd></div>
                <div><dt>Source</dt><dd>{skillPreview.sourceLabel}</dd></div>
              </dl>
            ) : null}
            {kind === "skill" && selected?.editable && !creating ? (
              <p className="skill-export-boundary">Export downloads only the exact saved SKILL.md content. CareerPilot adds no credentials, workspace paths, or hidden metadata.</p>
            ) : null}
            <label htmlFor={`agent-resource-name-${kind}`}>{skillPreview ? "Install name" : "File name"}</label>
            <input
              aria-describedby={skillPreview && skillInstallReadiness?.error ? "skill-preview-validation" : undefined}
              id={`agent-resource-name-${kind}`}
              disabled={readOnly || (!creating && Boolean(selected)) || saving}
              maxLength={agentAssetNameMaxLength(kind, creating)}
              onChange={(event) => changeName(event.target.value)}
              spellCheck={false}
              value={draftName}
            />
            <label htmlFor={`agent-resource-content-${kind}`}>{skillPreview ? "Complete SKILL.md content" : "Markdown content"}</label>
            <textarea
              id={`agent-resource-content-${kind}`}
              className="agent-resource-content"
              disabled={saving}
              onChange={(event) => setDraftContent(event.target.value)}
              readOnly={readOnly || Boolean(skillPreview)}
              spellCheck={false}
              value={draftContent}
            />
            {skillPreview && skillInstallReadiness?.error ? (
              <p className="skill-preview-validation" id="skill-preview-validation" role="alert">{skillInstallReadiness.error} The preview remains unchanged until you choose another valid name.</p>
            ) : null}
            {!readOnly ? (
              <div className="agent-resource-actions">
                <p>{skillPreview ? "Preview only. Nothing is written or activated until you explicitly install this skill." : "Saved locally. Changes restart Pilot’s local runtime without exposing these files to the browser again."}</p>
                <button className="auth-action" disabled={saving || !validName || !draftContent.trim() || Boolean(skillPreview && !skillInstallReadiness?.canInstall)} onClick={() => void save()} type="button">
                  {saving ? (skillPreview ? "Installing…" : "Saving…") : skillPreview ? "Install skill" : creating ? `Create ${kind}` : "Save changes"}
                </button>
              </div>
            ) : (
              <p className="agent-resource-readonly-note">Built-in skills are supplied by CareerPilot and cannot be changed from this workspace.</p>
            )}
          </div>
        </div>
      ) : loadState.status === "loading" ? (
        <p className="settings-loading" role="status">Loading agent files…</p>
      ) : null}

      {notice ? <div className="capability-notice" role="status">{notice}</div> : null}
      {loadState.error ? (
        <div className="settings-recovery-error" role="alert">
          <div>
            <strong>Agent workspace unavailable</strong>
            <span>{loadState.error} Your selected file, edits, and skill preview are preserved.</span>
          </div>
          <button
            className="secondary-action"
            disabled={loadState.status === "loading"}
            onClick={() => setLoadRetry(nextReadGeneration)}
            type="button"
          >
            {loadState.status === "loading" ? "Retrying…" : "Retry memories & skills"}
          </button>
        </div>
      ) : null}
      {error ? <div className="auth-error" role="alert"><strong>Agent workspace</strong><span>{error}</span></div> : null}
    </section>
  );
}
