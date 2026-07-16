"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";

import type {
  CapabilityGroup,
  CapabilitySettingsResponse,
} from "./types";


interface CapabilitySettingsProps {
  apiBaseUrl: string;
}

async function readError(response: Response, fallback: string): Promise<string> {
  const payload = (await response.json().catch(() => null)) as
    | { detail?: string }
    | null;
  return payload?.detail ?? fallback;
}

const TOOL_LABELS: Record<string, string> = {
  career_profile_get: "Reviewed profile",
  career_job_add: "Add jobs",
  career_job_score: "Score jobs",
  career_job_queue: "Job queue",
  career_application_queue: "Application queue",
  career_application_status: "Application status",
  career_revision_propose: "Revision proposals",
  career_policy_status: "Policy status",
  career_browser_fill: "Approved form fill",
  read_file: "Read files",
  write_file: "Write files",
  patch: "Edit files",
  terminal: "Local commands",
  execute_code: "Run code",
  web_search: "Search the web",
  delegation: "Delegation",
  messaging: "Messaging",
  browser: "General browser control",
  memory: "Raw memory writes",
  cronjob: "Schedules",
};

function toolLabel(tool: string): string {
  return TOOL_LABELS[tool] ?? tool.replaceAll("_", " ");
}

function CapabilityCard({ group }: { group: CapabilityGroup }) {
  return (
    <article className={`capability-card capability-${group.state}`}>
      <div className="capability-card-heading">
        <div>
          <h3>{group.name}</h3>
          <p>{group.description}</p>
        </div>
        <span className="capability-state">{group.state_label}</span>
      </div>
      <div className="tool-chip-list" aria-label={`${group.name} tool list`}>
        {group.tools.map((tool) => (
          <span key={tool} title={tool}>{toolLabel(tool)}</span>
        ))}
      </div>
      {group.note ? <p className="capability-note">{group.note}</p> : null}
    </article>
  );
}

export function CapabilitySettings({ apiBaseUrl }: CapabilitySettingsProps) {
  const [settings, setSettings] = useState<CapabilitySettingsResponse | null>(null);
  const [searchKey, setSearchKey] = useState("");
  const [savingSearch, setSavingSearch] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    async function loadSettings() {
      try {
        const response = await fetch(`${apiBaseUrl}/settings/capabilities`, {
          credentials: "include",
        });
        if (!response.ok) {
          throw new Error(await readError(response, "Capabilities could not load."));
        }
        const payload = (await response.json()) as CapabilitySettingsResponse;
        if (active) setSettings(payload);
      } catch (caughtError) {
        if (active) {
          setError(
            caughtError instanceof Error
              ? caughtError.message
              : "Capabilities could not load.",
          );
        }
      }
    }
    void loadSettings();
    return () => {
      active = false;
    };
  }, [apiBaseUrl]);

  const enabledGroups = useMemo(
    () => settings?.groups.filter((group) => group.state === "enabled").length ?? 0,
    [settings],
  );
  const enabledMcp = useMemo(
    () => settings?.mcp_servers.filter((server) => server.enabled).length ?? 0,
    [settings],
  );

  async function connectWebSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setNotice(null);
    setSavingSearch(true);
    try {
      const response = await fetch(`${apiBaseUrl}/settings/capabilities/web-search`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: searchKey.trim() }),
      });
      if (!response.ok) {
        throw new Error(await readError(response, "Web search could not be enabled."));
      }
      setSettings((await response.json()) as CapabilitySettingsResponse);
      setSearchKey("");
      setNotice("Web search is ready. Pilot can now find current public pages and job links.");
    } catch (caughtError) {
      setError(
        caughtError instanceof Error
          ? caughtError.message
          : "Web search could not be enabled.",
      );
    } finally {
      setSavingSearch(false);
    }
  }

  async function disconnectWebSearch() {
    setError(null);
    setNotice(null);
    setSavingSearch(true);
    try {
      const response = await fetch(`${apiBaseUrl}/settings/capabilities/web-search`, {
        method: "DELETE",
        credentials: "include",
      });
      if (!response.ok) {
        throw new Error(await readError(response, "Web search could not be removed."));
      }
      setSettings((await response.json()) as CapabilitySettingsResponse);
      setNotice("Web search was turned off and its saved key was removed.");
    } catch (caughtError) {
      setError(
        caughtError instanceof Error
          ? caughtError.message
          : "Web search could not be removed.",
      );
    } finally {
      setSavingSearch(false);
    }
  }

  return (
    <section className="auth-panel capability-panel" aria-labelledby="capability-title">
      <div className="auth-panel-heading">
        <span className="eyebrow">Pilot capabilities</span>
        <h2 id="capability-title">See exactly what Pilot can use</h2>
        <p>
          Ready means available now. Limited tools pause for your approval, and
          anything off stays outside Pilot’s reach.
        </p>
      </div>

      {settings ? (
        <>
          <div className="capability-summary" aria-label="Capability summary">
            <div><strong>{enabledGroups}</strong><span>ready groups</span></div>
            <div><strong>{enabledMcp}</strong><span>MCP enabled</span></div>
            <div>
              <strong>{settings.web_search.configured ? "On" : "Off"}</strong>
              <span>web search</span>
            </div>
          </div>

          <div className="capability-grid">
            {settings.groups.map((group) => (
              <CapabilityCard key={group.id} group={group} />
            ))}
          </div>

          <section className="capability-setup" aria-labelledby="web-search-title">
            <div className="capability-section-heading">
              <div>
                <span className="eyebrow">Guided setup</span>
                <h3 id="web-search-title">Web search</h3>
              </div>
              <span className={settings.web_search.configured ? "is-ready" : "needs-setup"}>
                {settings.web_search.configured ? "Connected" : "Not connected"}
              </span>
            </div>
            {settings.web_search.configured ? (
              <div className="connected-capability">
                <div>
                  <strong>{settings.web_search.provider_label}</strong>
                  <p>
                    The key is encrypted locally and shared only with Pilot’s search
                    process. Search results are treated as untrusted content.
                  </p>
                </div>
                <button
                  className="text-action danger-action"
                  type="button"
                  disabled={savingSearch}
                  onClick={() => void disconnectWebSearch()}
                >
                  {savingSearch ? "Removing…" : "Remove search key"}
                </button>
              </div>
            ) : (
              <div className="web-search-onboarding">
                <ol>
                  <li>
                    <span>1</span>
                    <p>
                      Create a key on the{" "}
                      <a href={settings.web_search.setup_url} target="_blank" rel="noreferrer">
                        Brave Search API site <span aria-hidden="true">↗</span>
                      </a>.
                    </p>
                  </li>
                  <li><span>2</span><p>Paste the key here. CareerPilot encrypts it before storage.</p></li>
                  <li><span>3</span><p>Ask Pilot to search for current public jobs.</p></li>
                </ol>
                <form className="api-key-form" onSubmit={connectWebSearch}>
                  <label htmlFor="brave-search-key">Brave Search API key</label>
                  <div className="api-key-row">
                    <input
                      id="brave-search-key"
                      type="password"
                      value={searchKey}
                      minLength={16}
                      maxLength={512}
                      required
                      autoComplete="off"
                      spellCheck={false}
                      placeholder="Paste your search key"
                      disabled={savingSearch}
                      onChange={(event) => setSearchKey(event.target.value)}
                    />
                    <button
                      className="auth-action auth-action-light"
                      type="submit"
                      disabled={savingSearch || searchKey.trim().length < 16}
                    >
                      {savingSearch ? "Saving…" : "Enable web search"}
                    </button>
                  </div>
                </form>
                <p className="capability-fine-print">
                  Search can find public job pages; LinkedIn browsing and application
                  submission remain manual.
                </p>
              </div>
            )}
          </section>

          <section className="mcp-section" aria-labelledby="mcp-title">
            <div className="capability-section-heading">
              <div>
                <span className="eyebrow">Connected services</span>
                <h3 id="mcp-title">MCP servers</h3>
              </div>
              <span className={enabledMcp > 0 ? "is-ready" : "needs-setup"}>
                {enabledMcp > 0 ? `${enabledMcp} enabled` : "None enabled"}
              </span>
            </div>
            <p className="mcp-explainer">
              MCP connections give Pilot selected tools from another service. Only the
              listed tools are allowed.
            </p>
            <div className="mcp-list">
              {settings.mcp_servers.length > 0 ? settings.mcp_servers.map((server) => (
                <article key={server.name} className="mcp-row">
                  <div>
                    <strong>{server.display_name}</strong>
                    <small>{server.transport.toUpperCase()}</small>
                  </div>
                  <div className="tool-chip-list">
                    {server.tools.map((tool) => <span key={tool} title={tool}>{toolLabel(tool)}</span>)}
                  </div>
                  <span className={server.enabled ? "mcp-enabled" : "mcp-disabled"}>
                    {server.enabled ? "Enabled" : "Off"}
                  </span>
                </article>
              )) : (
                <div className="mcp-empty">No MCP servers are configured.</div>
              )}
            </div>
          </section>
        </>
      ) : !error ? <p className="settings-loading">Loading capabilities…</p> : null}

      {notice ? <div className="capability-notice" role="status">{notice}</div> : null}
      {error ? (
        <div className="auth-error" role="alert">
          <strong>Capability settings unavailable</strong>
          <span>{error}</span>
        </div>
      ) : null}
    </section>
  );
}
