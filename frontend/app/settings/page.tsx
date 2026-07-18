"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { CapabilitySettings } from "../capability-settings";
import { AgentResourceSettings } from "../agent-resource-settings";
import { AgentIdentitySettings } from "../agent-identity-settings";
import { MCPSettings } from "../mcp-settings";
import { ProviderSettings } from "../provider-settings";
import type { AuthSessionResponse, AuthUser } from "../types";


const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ??
  "";

export default function SettingsPage() {
  const router = useRouter();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [capabilityRefresh, setCapabilityRefresh] = useState(0);

  useEffect(() => {
    let active = true;
    async function restoreSession() {
      try {
        const response = await fetch(`${API_BASE_URL}/local/session`);
        const payload = response.ok
          ? (await response.json()) as AuthSessionResponse
          : null;
        if (!active) return;
        if (!payload?.authenticated || !payload.user) {
          router.replace("/");
          return;
        }
        setUser(payload.user);
      } catch {
        if (active) router.replace("/");
      } finally {
        if (active) setLoading(false);
      }
    }
    void restoreSession();
    return () => {
      active = false;
    };
  }, [router]);

  if (loading || !user) {
    return (
      <main className="auth-shell auth-loading" aria-live="polite">
        <div className="loading-orbit" aria-hidden="true"><span /></div>
        <p>Loading your settings…</p>
      </main>
    );
  }

  return (
    <main>
      <header className="site-header">
        <Link className="brand" href="/" aria-label="CareerPilot home">
          <span className="brand-mark" aria-hidden="true">CP</span>
          <span>CareerPilot</span>
        </Link>
        <div className="account-control">
          <span className="account-identity">Local-only workspace</span>
          <Link className="header-action" href="/workspace">Workspace</Link>
          {user.active_provider ? (
            <Link className="header-action" href="/">Return to Pilot</Link>
          ) : null}
        </div>
      </header>

      <div className="settings-shell">
        <section className="settings-intro">
          <span className="eyebrow">Settings</span>
          <h1>Choose how Pilot thinks—and what it can use.</h1>
          <p>
            CareerPilot runs locally without a CareerPilot account. Connect an OpenAI
            API key, or use an eligible ChatGPT plan through a compatible Codex runtime
            already installed on this device. Then review every tool and connected
            service available to Pilot.
          </p>
        </section>
        <div className="settings-stack">
          <ProviderSettings
            apiBaseUrl={API_BASE_URL}
            user={user}
            onUpdated={setUser}
          />
          <AgentIdentitySettings apiBaseUrl={API_BASE_URL} />
          <AgentResourceSettings apiBaseUrl={API_BASE_URL} />
          <MCPSettings
            apiBaseUrl={API_BASE_URL}
            onUpdated={() => setCapabilityRefresh((current) => current + 1)}
          />
          <CapabilitySettings apiBaseUrl={API_BASE_URL} refreshKey={capabilityRefresh} />
        </div>
      </div>
    </main>
  );
}
