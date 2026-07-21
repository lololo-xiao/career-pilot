"use client";

import {
  KeyboardEvent,
  useEffect,
  useReducer,
  useRef,
  useState,
} from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { CapabilitySettings } from "../capability-settings";
import { API_BASE_URL } from "../api-base-url";
import { AgentResourceSettings } from "../agent-resource-settings";
import { AgentIdentitySettings } from "../agent-identity-settings";
import { BrandMark } from "../brand-mark";
import { MCPSettings } from "../mcp-settings";
import { NativeBackendControl } from "../native-app-shell";
import { ProviderSettings } from "../provider-settings";
import {
  createSettingsReadController,
  getSettingsNavigationIntent,
  initialRecoverableReadState,
  isAuthSessionResponse,
  nextReadGeneration,
  recoverableReadReducer,
  SETTINGS_STEPS,
  settingsNavigatorReducer,
  startRecoverableSettingsRead,
  shouldRedirectForSession,
  type SettingsNavigationMode,
  type SettingsStepId,
} from "../settings-recovery";
import type { AuthSessionResponse, AuthUser } from "../types";


const NEXT_STEP_COPY: Record<SettingsStepId, string> = {
  "ai-connection": "Connection ready? Shape how your companion sounds and collaborates.",
  "identity-soul": "Identity saved? Review the memories and skills Pilot can carry forward.",
  "memories-skills": "Context reviewed? Add outside tools only if you need them.",
  mcp: "MCP is optional. Continue to confirm Pilot’s complete capability boundary.",
  "capability-review": "Review complete. You can revisit any step without losing a draft.",
};

function stepHeading(step: SettingsStepId): HTMLElement | null {
  const targetId = SETTINGS_STEPS.find((candidate) => candidate.id === step)?.targetId;
  if (!targetId) return null;
  return document.querySelector<HTMLElement>(`#${targetId} h2`);
}

export default function SettingsPage() {
  const router = useRouter();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [sessionRetry, setSessionRetry] = useState(0);
  const [sessionController] = useState(
    () => createSettingsReadController("session"),
  );
  const [sessionRead, dispatchSessionRead] = useReducer(
    recoverableReadReducer<AuthSessionResponse>,
    undefined,
    () => initialRecoverableReadState<AuthSessionResponse>(),
  );
  const [navigator, dispatchNavigator] = useReducer(settingsNavigatorReducer, {
    currentStep: SETTINGS_STEPS[0].id,
  });
  const navigatorButtonRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const [capabilityRefresh, setCapabilityRefresh] = useState(0);

  useEffect(() => {
    const abortController = new AbortController();
    const read = startRecoverableSettingsRead({
      apiBaseUrl: API_BASE_URL,
      controller: sessionController,
      fallback: "Local session could not load.",
      onFailure(error, generation) {
        dispatchSessionRead({ type: "failure", generation, error });
      },
      onStart(generation) {
        dispatchSessionRead({ type: "start", generation });
      },
      onSuccess(payload, generation) {
        if (shouldRedirectForSession(payload)) {
          router.replace("/");
          return;
        }
        dispatchSessionRead({ type: "success", generation, data: payload });
        setUser(payload.user);
      },
      signal: abortController.signal,
      validate: isAuthSessionResponse,
    });
    void read.completion;
    return () => {
      abortController.abort();
      sessionController.invalidate(read.ticket);
    };
  }, [router, sessionController, sessionRetry]);

  useEffect(() => {
    const hash = window.location.hash.slice(1);
    const requested = SETTINGS_STEPS.find((step) => step.targetId === hash);
    if (requested) dispatchNavigator({ type: "select", step: requested.id });
  }, []);

  function presentStep(
    step: SettingsStepId,
    mode: SettingsNavigationMode = "activate",
  ) {
    const intent = getSettingsNavigationIntent(mode);
    dispatchNavigator({ type: "select", step });
    const target = SETTINGS_STEPS.find((candidate) => candidate.id === step);
    if (!target) return;
    if (intent.updateHash) {
      window.history.replaceState(null, "", `#${target.targetId}`);
    }
    if (!intent.scrollContent && !intent.focusContent) return;
    window.requestAnimationFrame(() => {
      if (intent.scrollContent) {
        document.getElementById(target.targetId)?.scrollIntoView({ block: "start" });
      }
      if (intent.focusContent) stepHeading(step)?.focus({ preventScroll: true });
    });
  }

  function handleNavigatorKeyDown(
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) {
    let nextIndex: number | null = null;
    if (event.key === "ArrowDown" || event.key === "ArrowRight") {
      nextIndex = Math.min(index + 1, SETTINGS_STEPS.length - 1);
    }
    if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
      nextIndex = Math.max(index - 1, 0);
    }
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = SETTINGS_STEPS.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    const step = SETTINGS_STEPS[nextIndex];
    presentStep(step.id, "roving");
    navigatorButtonRefs.current[nextIndex]?.focus();
  }

  return (
    <main>
      <header className="site-header">
        <Link className="brand" href="/" aria-label="CareerPilot home">
          <span className="brand-mark" aria-hidden="true"><BrandMark /></span>
          <span>CareerPilot</span>
        </Link>
        <div className="account-control">
          <span className="account-identity">Private workspace</span>
          <NativeBackendControl />
          <Link className="header-action" href="/privacy">Privacy</Link>
          <Link className="header-action" href="/workspace">Workspace</Link>
          {user?.active_provider ? (
            <Link className="header-action" href="/">Return to Pilot</Link>
          ) : null}
        </div>
      </header>

      <div className="settings-shell">
        <aside className="settings-intro">
          <span className="eyebrow">Settings</span>
          <h1>Choose how Pilot thinks—and what it can use.</h1>
          <p>
            CareerPilot runs in your private runtime without a CareerPilot account. Connect an OpenAI
            API key, or use an eligible ChatGPT plan through a compatible Codex runtime
            already installed on the runtime host. Then review every tool and connected
            service available to Pilot.
          </p>

          {sessionRead.status === "loading" && !user ? (
            <p className="settings-session-loading" role="status">
              Checking your private workspace session…
            </p>
          ) : null}
          {sessionRead.error ? (
            <div className="settings-recovery-error settings-session-error" role="alert">
              <div>
                <strong>Workspace session unavailable</strong>
                <span>{sessionRead.error} Your settings and drafts remain on this page.</span>
              </div>
              <button
                className="secondary-action"
                disabled={sessionRead.status === "loading"}
                onClick={() => setSessionRetry(nextReadGeneration)}
                type="button"
              >
                {sessionRead.status === "loading" ? "Retrying…" : "Retry session"}
              </button>
            </div>
          ) : null}

          <nav className="settings-navigator" aria-label="Settings setup steps">
            <ol>
              {SETTINGS_STEPS.map((step, index) => {
                const current = navigator.currentStep === step.id;
                return (
                  <li key={step.id}>
                    <button
                      aria-controls={step.targetId}
                      aria-current={current ? "step" : undefined}
                      className={current ? "is-current" : ""}
                      onClick={() => presentStep(step.id)}
                      onKeyDown={(event) => handleNavigatorKeyDown(event, index)}
                      ref={(node) => {
                        navigatorButtonRefs.current[index] = node;
                      }}
                      tabIndex={current ? 0 : -1}
                      type="button"
                    >
                      <span aria-hidden="true">{index + 1}</span>
                      <span>
                        <strong>{step.label}</strong>
                        <small>{step.description}</small>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ol>
            <p>Arrow keys move between steps. Enter opens a step and moves focus to it.</p>
          </nav>
        </aside>

        <div className="settings-stack">
          <div className={`settings-step${navigator.currentStep === "ai-connection" ? " is-current" : ""}`}>
            <ProviderSettings
              apiBaseUrl={API_BASE_URL}
              user={user}
              onUpdated={setUser}
            />
            <div className="settings-step-next">
              <p>{NEXT_STEP_COPY["ai-connection"]}</p>
              <button type="button" onClick={() => presentStep("identity-soul")}>
                Continue to Identity / Soul <span aria-hidden="true">↓</span>
              </button>
            </div>
          </div>

          <div className={`settings-step${navigator.currentStep === "identity-soul" ? " is-current" : ""}`}>
            <AgentIdentitySettings apiBaseUrl={API_BASE_URL} />
            <div className="settings-step-next">
              <p>{NEXT_STEP_COPY["identity-soul"]}</p>
              <button type="button" onClick={() => presentStep("memories-skills")}>
                Continue to Memories & Skills <span aria-hidden="true">↓</span>
              </button>
            </div>
          </div>

          <div className={`settings-step${navigator.currentStep === "memories-skills" ? " is-current" : ""}`}>
            <AgentResourceSettings apiBaseUrl={API_BASE_URL} />
            <div className="settings-step-next">
              <p>{NEXT_STEP_COPY["memories-skills"]}</p>
              <button type="button" onClick={() => presentStep("mcp")}>
                Continue to optional MCP <span aria-hidden="true">↓</span>
              </button>
            </div>
          </div>

          <div className={`settings-step${navigator.currentStep === "mcp" ? " is-current" : ""}`}>
            <MCPSettings
              apiBaseUrl={API_BASE_URL}
              onUpdated={() => setCapabilityRefresh((current) => current + 1)}
            />
            <div className="settings-step-next">
              <p>{NEXT_STEP_COPY.mcp}</p>
              <button type="button" onClick={() => presentStep("capability-review")}>
                Continue to capability review <span aria-hidden="true">↓</span>
              </button>
            </div>
          </div>

          <div className={`settings-step${navigator.currentStep === "capability-review" ? " is-current" : ""}`}>
            <CapabilitySettings apiBaseUrl={API_BASE_URL} refreshKey={capabilityRefresh} />
            <div className="settings-step-next settings-step-finish">
              <p>{NEXT_STEP_COPY["capability-review"]}</p>
              <div>
                <button type="button" onClick={() => presentStep("ai-connection")}>
                  Review from the start <span aria-hidden="true">↑</span>
                </button>
                <Link href={user?.active_provider ? "/" : "/workspace"}>
                  {user?.active_provider ? "Return to Pilot" : "Open workspace"}
                </Link>
              </div>
            </div>
          </div>
        </div>
      </div>
    </main>
  );
}
