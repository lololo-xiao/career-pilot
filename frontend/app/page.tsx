"use client";

import { FormEvent, KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { explainApproval, type ApprovalRequest } from "./approval-explanation";
import { ResultView } from "./result-view";
import type {
  AgentSettingsResponse,
  AuthSessionResponse,
  AuthUser,
  MatchResponse,
  ParsedCVResponse,
  ReasoningEffort,
} from "./types";


const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ??
  "";

const SAMPLE_PROFILE = `AI engineer with 4 years of Python experience. Built retrieval-augmented generation applications using LangChain, Chroma, and FastAPI. Designed evaluation datasets for LLM quality and added tracing for prompt latency and token usage. Deployed Dockerized services to Azure Container Apps. Mentored two junior engineers.`;

const SAMPLE_JOB = `We are hiring a Senior AI Engineer to build production generative AI products. Required: strong Python, hands-on RAG architecture, vector database experience, API development, and systematic LLM evaluation. You should be comfortable owning services in production and communicating technical decisions. Preferred: Kubernetes, Azure, LangGraph, and experience mentoring engineers.`;

type EditorKind = "profile" | "role";

interface ChatMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  suggestions?: string[];
  report?: MatchResponse;
}

interface HermesRunEvent {
  event?: string;
  run_id?: string;
  delta?: string;
  output?: string;
  error?: string;
  tool?: string;
  command?: string;
  description?: string;
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  };
}

interface PendingRunApproval extends ApprovalRequest {
  runId: string;
}

interface ContextUsage {
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
}

interface IconProps {
  name: "home" | "briefcase" | "spark" | "paperclip" | "send" | "file" | "target" | "check" | "settings";
  size?: number;
}

function Icon({ name, size = 18 }: IconProps) {
  const paths: Record<IconProps["name"], React.ReactNode> = {
    home: <><path d="m3 10 9-7 9 7" /><path d="M5 9v11h14V9" /><path d="M9 20v-6h6v6" /></>,
    briefcase: <><rect x="3" y="7" width="18" height="13" rx="2" /><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 12h18M10 12v2h4v-2" /></>,
    spark: <><path d="m12 3 1.4 4.1L17.5 8.5l-4.1 1.4L12 14l-1.4-4.1-4.1-1.4 4.1-1.4L12 3Z" /><path d="m18.5 14 .7 2.3 2.3.7-2.3.7-.7 2.3-.7-2.3-2.3-.7 2.3-.7.7-2.3Z" /></>,
    paperclip: <path d="m20.5 11.5-8.8 8.8a6 6 0 0 1-8.5-8.5l9.5-9.5a4 4 0 0 1 5.7 5.7l-9.5 9.5a2 2 0 1 1-2.8-2.8l8.8-8.8" />,
    send: <><path d="m22 2-7 20-4-9-9-4 20-7Z" /><path d="M22 2 11 13" /></>,
    file: <><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z" /><path d="M14 2v6h6M8 13h8M8 17h6" /></>,
    target: <><circle cx="12" cy="12" r="9" /><circle cx="12" cy="12" r="4" /><path d="m15 9 6-6M17 3h4v4" /></>,
    check: <path d="m5 12 4 4L19 6" />,
    settings: <><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z" /></>,
  };
  return (
    <svg aria-hidden="true" fill="none" height={size} viewBox="0 0 24 24" width={size} stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8">
      {paths[name]}
    </svg>
  );
}

function AgentAvatar({ small = false }: { small?: boolean }) {
  return (
    <span className={`pilot-avatar${small ? " pilot-avatar-small" : ""}`} aria-hidden="true">
      <span>P</span>
      <i />
    </span>
  );
}

function ApprovalCard({
  approval,
  isResolving,
  onRespond,
}: {
  approval: PendingRunApproval;
  isResolving: boolean;
  onRespond: (choice: "once" | "deny") => void;
}) {
  const explanation = explainApproval(approval);
  return (
    <div className="pilot-approval-card">
      <div className="pilot-approval-summary">
        <span>Requested action</span>
        <h3>{explanation.summary}</h3>
        <p>{explanation.reason}</p>
      </div>
      <dl className="pilot-approval-meta">
        <div><dt>Tool</dt><dd>{explanation.tool}</dd></div>
        <div><dt>Behavior</dt><dd>{explanation.behavior}</dd></div>
        <div><dt>Permission</dt><dd>{explanation.scope}</dd></div>
      </dl>
      <details className="pilot-approval-technical">
        <summary>Show technical details and code <span aria-hidden="true">⌄</span></summary>
        <div className="pilot-approval-system-note">
          <strong>Why the system paused</strong>
          <p>{approval.description}</p>
        </div>
        <pre><code>{approval.command}</code></pre>
      </details>
      <div className="pilot-approval-actions">
        <button type="button" onClick={() => onRespond("deny")} disabled={isResolving}>Don’t run</button>
        <button className="pilot-approval-allow" type="button" onClick={() => onRespond("once")} disabled={isResolving}>{isResolving ? "Applying…" : "Allow once"}</button>
      </div>
    </div>
  );
}

function MatchBrief({ report, reportId }: { report: MatchResponse; reportId: number }) {
  const strongest = report.matched_skills.slice(0, 3);
  const gaps = report.missing_skills.slice(0, 3);
  const nextAction = [...report.preparation_actions].sort(
    (a, b) => a.priority - b.priority,
  )[0];

  return (
    <div className="pilot-match-card">
      <div className="pilot-match-topline">
        <div className="pilot-score" style={{ "--pilot-score": `${report.score * 10}%` } as React.CSSProperties}>
          <span><strong>{report.score}</strong>/10</span>
        </div>
        <div>
          <span className="pilot-kicker">Grounded fit check</span>
          <h3>{report.score >= 7 ? "This role is worth pursuing." : report.score >= 5 ? "There is a credible path here." : "This one may be a stretch—for now."}</h3>
          <p>{report.summary}</p>
        </div>
      </div>

      <div className="pilot-match-counts">
        <span><strong>{report.matched_skills.length}</strong> direct matches</span>
        <span><strong>{report.adjacent_skills.length}</strong> transferable</span>
        <span><strong>{report.missing_skills.length}</strong> gaps to mind</span>
      </div>

      <div className="pilot-match-columns">
        <div>
          <span className="pilot-mini-label pilot-mini-label-good">What already works</span>
          {strongest.length ? strongest.map((item) => (
            <p key={item.skill}><Icon name="check" size={15} /> <span><strong>{item.skill}</strong>{item.explanation}</span></p>
          )) : <p className="pilot-muted">No direct evidence yet.</p>}
        </div>
        <div>
          <span className="pilot-mini-label pilot-mini-label-gap">What to be honest about</span>
          {gaps.length ? gaps.map((item) => (
            <p key={item.skill}><span className="pilot-gap-dot" /> <span><strong>{item.skill}</strong>{item.explanation}</span></p>
          )) : <p className="pilot-muted">No meaningful gaps surfaced.</p>}
        </div>
      </div>

      {nextAction ? (
        <div className="pilot-next-move">
          <span>01</span>
          <div><small>Our next move</small><strong>{nextAction.action}</strong><p>{nextAction.rationale}</p></div>
        </div>
      ) : null}

      <details className="pilot-report-details">
        <summary>Open the full evidence report <span>↗</span></summary>
        <ResultView report={report} titleId={`pilot-report-${reportId}`} />
      </details>
    </div>
  );
}

async function readApiError(response: Response, fallback: string) {
  const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
  return payload?.detail ?? fallback;
}

function effortLabel(effort: ReasoningEffort) {
  if (effort === "xhigh") return "Extra high";
  if (effort === "none") return "None";
  return `${effort.charAt(0).toUpperCase()}${effort.slice(1)}`;
}

function limitWindowLabel(minutes: number | null) {
  if (!minutes) return "Usage window";
  if (minutes % 10_080 === 0) return `${minutes / 10_080}-week window`;
  if (minutes % 1_440 === 0) return `${minutes / 1_440}-day window`;
  if (minutes % 60 === 0) return `${minutes / 60}-hour window`;
  return `${minutes}-minute window`;
}

function resetLabel(timestamp: number | null) {
  if (!timestamp) return "Reset time unavailable";
  return `Resets ${new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(timestamp * 1000))}`;
}

export default function Home() {
  const router = useRouter();
  const [authUser, setAuthUser] = useState<AuthUser | null>(null);
  const [isCheckingSession, setIsCheckingSession] = useState(true);
  const [candidateProfile, setCandidateProfile] = useState("");
  const [jobDescription, setJobDescription] = useState("");
  const [uploadedFilename, setUploadedFilename] = useState<string | null>(null);
  const [report, setReport] = useState<MatchResponse | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [isChatting, setIsChatting] = useState(false);
  const [chatActivity, setChatActivity] = useState<string | null>(null);
  const [pendingApproval, setPendingApproval] = useState<PendingRunApproval | null>(null);
  const [isResolvingApproval, setIsResolvingApproval] = useState(false);
  const [agentSettings, setAgentSettings] = useState<AgentSettingsResponse | null>(null);
  const [agentSettingsError, setAgentSettingsError] = useState<string | null>(null);
  const [isLoadingAgentSettings, setIsLoadingAgentSettings] = useState(false);
  const [isSavingAgentSettings, setIsSavingAgentSettings] = useState(false);
  const [isAgentSettingsDirty, setIsAgentSettingsDirty] = useState(false);
  const [contextUsage, setContextUsage] = useState<ContextUsage | null>(null);
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [isParsingCV, setIsParsingCV] = useState(false);
  const [editor, setEditor] = useState<EditorKind | null>(null);
  const [editorDraft, setEditorDraft] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const messageIdRef = useRef(1);
  const conversationEndRef = useRef<HTMLDivElement>(null);

  function nextMessage(
    role: ChatMessage["role"],
    content: string,
    extra: Partial<Pick<ChatMessage, "suggestions" | "report">> = {},
  ): ChatMessage {
    return { id: messageIdRef.current++, role, content, ...extra };
  }

  const openWorkspace = useCallback((user: AuthUser) => {
    setAuthUser(user);
    setMessages((current) => {
      if (current.length) return current;
      return [{
        id: messageIdRef.current++,
        role: "assistant",
        content: "Hey—I’m Pilot. I’m here to make the job search feel less like something you have to carry alone.\n\nBring me a direction, a role, or just the part that feels stuck. We’ll take it one honest next step at a time.",
      }];
    });
    setSuggestions(["Load the demo workspace", "Add my career profile", "Help me choose a target role"]);
  }, []);

  useEffect(() => {
    let active = true;
    async function restoreSession() {
      try {
        const response = await fetch(`${API_BASE_URL}/local/session`);
        if (!response.ok) return;
        const payload = (await response.json()) as AuthSessionResponse;
        if (active && payload.authenticated && payload.user) openWorkspace(payload.user);
      } catch {
        if (active) setAuthUser(null);
      } finally {
        if (active) setIsCheckingSession(false);
      }
    }
    void restoreSession();
    return () => { active = false; };
  }, [openWorkspace]);

  useEffect(() => {
    if (!isCheckingSession && authUser && !authUser.active_provider) router.replace("/settings");
  }, [authUser, isCheckingSession, router]);

  const loadAgentSettings = useCallback(async () => {
    setIsLoadingAgentSettings(true);
    setAgentSettingsError(null);
    try {
      const response = await fetch(`${API_BASE_URL}/settings/agent`, {
        credentials: "include",
        cache: "no-store",
      });
      if (!response.ok) {
        throw new Error(await readApiError(response, "Pilot’s model settings are unavailable."));
      }
      const payload = (await response.json()) as AgentSettingsResponse;
      setAgentSettings(payload);
      setIsAgentSettingsDirty(false);
    } catch (caughtError) {
      setAgentSettingsError(
        caughtError instanceof Error ? caughtError.message : "Pilot’s model settings are unavailable.",
      );
    } finally {
      setIsLoadingAgentSettings(false);
    }
  }, []);

  useEffect(() => {
    if (!authUser?.active_provider) return;
    const timeout = window.setTimeout(() => void loadAgentSettings(), 0);
    return () => window.clearTimeout(timeout);
  }, [authUser?.active_provider, loadAgentSettings]);

  useEffect(() => {
    conversationEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, isChatting, isAnalyzing, chatActivity, pendingApproval]);

  function openEditor(kind: EditorKind) {
    setEditor(kind);
    setEditorDraft(kind === "profile" ? candidateProfile : jobDescription);
    setError(null);
  }

  function saveEditor() {
    if (editorDraft.trim().length < 10 || !editor) {
      setError("Add a little more detail before saving this to Pilot’s working memory.");
      return;
    }
    if (editor === "profile") {
      setCandidateProfile(editorDraft.trim());
      setUploadedFilename(null);
      setMessages((current) => [...current, nextMessage("assistant", "Got it—I’ve added your career evidence to our working memory. I’ll use it to keep our next steps specific and honest.")]);
      setSuggestions(jobDescription ? ["Run the fit check", "What stands out in my profile?"] : ["Add a target role", "What stands out in my profile?"]);
    } else {
      setJobDescription(editorDraft.trim());
      setMessages((current) => [...current, nextMessage("assistant", "I’ve got the role. We can unpack what it really asks for, or compare it with your evidence when you’re ready.")]);
      setSuggestions(candidateProfile ? ["Run the fit check", "What does this role value most?"] : ["Add my career profile", "What does this role value most?"]);
    }
    setEditor(null);
    setEditorDraft("");
    setReport(null);
    setError(null);
  }

  function loadDemo() {
    setCandidateProfile(SAMPLE_PROFILE);
    setJobDescription(SAMPLE_JOB);
    setUploadedFilename("demo-profile.txt");
    setReport(null);
    setMessages((current) => [...current, nextMessage("assistant", "Demo workspace loaded. I now have a real candidate profile and a Senior AI Engineer role in working memory. This is where the experience starts to feel like a partnership: you bring the context, and I help us decide what matters next.")]);
    setSuggestions(["Run the fit check", "What does this role value most?", "How should we position this profile?"]);
    setError(null);
  }

  function chooseAgentModel(model: string) {
    setAgentSettings((current) => {
      if (!current) return current;
      const option = current.models.find((item) => item.model === model);
      return {
        ...current,
        model,
        reasoning_effort: option?.default_reasoning_effort ?? current.reasoning_effort,
      };
    });
    setContextUsage(null);
    setIsAgentSettingsDirty(true);
    setAgentSettingsError(null);
  }

  function chooseReasoningEffort(reasoningEffort: ReasoningEffort) {
    setAgentSettings((current) => current ? {
      ...current,
      reasoning_effort: reasoningEffort,
    } : current);
    setIsAgentSettingsDirty(true);
    setAgentSettingsError(null);
  }

  async function saveAgentSettings() {
    if (!agentSettings || isSavingAgentSettings) return;
    setIsSavingAgentSettings(true);
    setAgentSettingsError(null);
    try {
      const response = await fetch(`${API_BASE_URL}/settings/agent`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: agentSettings.model,
          reasoning_effort: agentSettings.reasoning_effort,
        }),
      });
      if (!response.ok) {
        throw new Error(await readApiError(response, "Pilot could not save these model settings."));
      }
      setAgentSettings((await response.json()) as AgentSettingsResponse);
      setContextUsage(null);
      setIsAgentSettingsDirty(false);
    } catch (caughtError) {
      setAgentSettingsError(
        caughtError instanceof Error ? caughtError.message : "Pilot could not save these model settings.",
      );
    } finally {
      setIsSavingAgentSettings(false);
    }
  }

  async function handleCVUpload(file: File) {
    setError(null);
    if (file.size > 5 * 1024 * 1024) {
      setError("CV uploads are limited to 5 MB.");
      return;
    }
    setIsParsingCV(true);
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 30_000);
    const formData = new FormData();
    formData.append("file", file);
    try {
      const response = await fetch(`${API_BASE_URL}/parse-cv`, {
        method: "POST",
        body: formData,
        signal: controller.signal,
        credentials: "include",
      });
      if (!response.ok) {
        if (response.status === 401) setAuthUser(null);
        throw new Error(await readApiError(response, "I couldn’t read that CV."));
      }
      const parsed = (await response.json()) as ParsedCVResponse;
      setCandidateProfile(parsed.text);
      setEditorDraft(parsed.text);
      setUploadedFilename(parsed.filename);
      setEditor(null);
      setReport(null);
      setMessages((current) => [...current, nextMessage("assistant", `I’ve read ${parsed.filename} and added its ${parsed.character_count.toLocaleString()} characters to our working memory. Give it a quick review whenever you like—then we can put it next to a role.`)]);
      setSuggestions(jobDescription ? ["Run the fit check", "Review my strongest evidence"] : ["Add a target role", "Review my strongest evidence"]);
    } catch (caughtError) {
      const message = caughtError instanceof DOMException && caughtError.name === "AbortError"
        ? "The CV took too long to parse. Try a smaller file or paste the text instead."
        : caughtError instanceof Error ? caughtError.message : "I couldn’t read that CV.";
      setError(message === "Failed to fetch" ? "Pilot couldn’t reach the CV parser. Check that the API is running." : message);
    } finally {
      window.clearTimeout(timeout);
      setIsParsingCV(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  async function sendChat(content: string) {
    const message = content.trim();
    if (!message || isChatting || isAnalyzing) return;
    const history = messages.slice(-10).map(({ role, content: turnContent }) => ({ role, content: turnContent }));
    setMessages((current) => [...current, nextMessage("user", message)]);
    setDraft("");
    setSuggestions([]);
    setError(null);
    setIsChatting(true);
    setPendingApproval(null);
    setChatActivity("Starting our companion session…");
    try {
      const response = await fetch(`${API_BASE_URL}/companion/chat/stream`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          conversation: history,
          candidate_profile: candidateProfile || null,
          job_description: jobDescription || null,
          match_report: report,
        }),
      });
      if (!response.ok) {
        if (response.status === 401) setAuthUser(null);
        if (response.status === 409) router.push("/settings");
        throw new Error(await readApiError(response, "Pilot couldn’t answer just now."));
      }
      if (!response.body) {
        throw new Error("Pilot opened a session but could not stream its reply.");
      }

      const assistantId = messageIdRef.current++;
      let assistantStarted = false;
      let assistantText = "";
      let completed = false;

      function showAssistant(nextText: string) {
        assistantText = nextText;
        if (!assistantStarted) {
          assistantStarted = true;
          setMessages((current) => [
            ...current,
            { id: assistantId, role: "assistant", content: nextText },
          ]);
          return;
        }
        setMessages((current) => current.map((item) => (
          item.id === assistantId ? { ...item, content: nextText } : item
        )));
      }

      function handleRunEvent(runEvent: HermesRunEvent) {
        if (runEvent.event === "run.started") {
          setChatActivity("Thinking this through with you…");
          return;
        }
        if (runEvent.event === "tool.started") {
          const tool = (runEvent.tool ?? "career context")
            .replace(/^career_/, "")
            .replaceAll("_", " ");
          setChatActivity(`Checking ${tool}…`);
          return;
        }
        if (runEvent.event === "tool.completed") {
          setChatActivity("Connecting the evidence…");
          return;
        }
        if (runEvent.event === "approval.request") {
          if (!runEvent.run_id) {
            throw new Error("Pilot requested approval without a valid task reference.");
          }
          setPendingApproval({
            runId: runEvent.run_id,
            command: runEvent.command ?? "Sensitive local action",
            description: runEvent.description ?? "Pilot needs your permission to continue this action.",
            tool: runEvent.tool,
          });
          setChatActivity(null);
          return;
        }
        if (runEvent.event === "approval.responded") {
          setPendingApproval(null);
          setChatActivity("Continuing the task…");
          return;
        }
        if (runEvent.event === "message.delta" && runEvent.delta) {
          setChatActivity(null);
          showAssistant(assistantText + runEvent.delta);
          return;
        }
        if (runEvent.event === "run.completed") {
          completed = true;
          setPendingApproval(null);
          setChatActivity(null);
          if (runEvent.usage) {
            setContextUsage({
              inputTokens: Math.max(0, runEvent.usage.input_tokens ?? 0),
              outputTokens: Math.max(0, runEvent.usage.output_tokens ?? 0),
              totalTokens: Math.max(0, runEvent.usage.total_tokens ?? 0),
            });
          }
          if (runEvent.output && runEvent.output !== assistantText) {
            showAssistant(runEvent.output);
          }
          return;
        }
        if (runEvent.event === "run.failed") {
          setPendingApproval(null);
          throw new Error(runEvent.error ?? "Pilot’s companion session failed.");
        }
      }

      function consumeBlock(block: string) {
        const lines = block.split(/\r?\n/);
        const eventName = lines
          .find((line) => line.startsWith("event:"))
          ?.slice(6)
          .trim();
        const data = lines
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart())
          .join("\n");
        if (!data) return;
        const runEvent = JSON.parse(data) as HermesRunEvent;
        if (!runEvent.event && eventName) runEvent.event = eventName;
        handleRunEvent(runEvent);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        const blocks = buffer.split(/\r?\n\r?\n/);
        buffer = blocks.pop() ?? "";
        blocks.forEach(consumeBlock);
        if (done) break;
      }
      if (buffer.trim()) consumeBlock(buffer);
      if (!completed || !assistantStarted || !assistantText.trim()) {
        throw new Error("Pilot’s companion session ended without a reply.");
      }
      setSuggestions(
        jobDescription
          ? ["What should we do next?", "Help me prepare for this role"]
          : ["Help me choose a target role", "What should we do next?"],
      );
    } catch (caughtError) {
      const message = caughtError instanceof Error ? caughtError.message : "Pilot couldn’t answer just now.";
      setError(message === "Failed to fetch" ? "Pilot couldn’t reach the API. Check that the backend is running." : message);
    } finally {
      setChatActivity(null);
      setPendingApproval(null);
      setIsResolvingApproval(false);
      setIsChatting(false);
    }
  }

  async function respondToApproval(choice: "once" | "deny") {
    if (!pendingApproval || isResolvingApproval) return;
    setIsResolvingApproval(true);
    setError(null);
    try {
      const response = await fetch(
        `${API_BASE_URL}/companion/chat/runs/${encodeURIComponent(pendingApproval.runId)}/approval`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ choice }),
        },
      );
      if (!response.ok) {
        throw new Error(await readApiError(response, "Pilot could not apply that approval."));
      }
      setPendingApproval(null);
      setChatActivity(choice === "once" ? "Continuing the approved action…" : "Skipping that action…");
    } catch (caughtError) {
      setError(caughtError instanceof Error ? caughtError.message : "Pilot could not apply that approval.");
    } finally {
      setIsResolvingApproval(false);
    }
  }

  async function runAnalysis() {
    if (isAnalyzing || isChatting) return;
    if (!candidateProfile) {
      openEditor("profile");
      setError("First, give Pilot a profile or CV to work from.");
      return;
    }
    if (!jobDescription) {
      openEditor("role");
      setError("Add a target role so Pilot has something concrete to compare.");
      return;
    }
    setMessages((current) => [...current, nextMessage("user", "Run a grounded fit check for this role.")]);
    setSuggestions([]);
    setError(null);
    setIsAnalyzing(true);
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 120_000);
    try {
      const response = await fetch(`${API_BASE_URL}/match`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ candidate_profile: candidateProfile, job_description: jobDescription }),
        signal: controller.signal,
      });
      if (!response.ok) {
        if (response.status === 401) setAuthUser(null);
        throw new Error(await readApiError(response, "The fit check couldn’t be completed."));
      }
      const match = (await response.json()) as MatchResponse;
      setReport(match);
      setMessages((current) => [...current, nextMessage(
        "assistant",
        "I’ve finished the evidence check. Here’s my honest read—not just where you match, but where we should be careful and what I’d do next.",
        { report: match },
      )]);
      setSuggestions(["Help me act on the first gap", "Prepare me for an interview", "How should I position my strengths?"]);
    } catch (caughtError) {
      const message = caughtError instanceof DOMException && caughtError.name === "AbortError"
        ? "The fit check timed out after two minutes. Try again with shorter inputs."
        : caughtError instanceof Error ? caughtError.message : "The fit check couldn’t be completed.";
      setError(message === "Failed to fetch" ? "Pilot couldn’t reach the analysis API." : message);
    } finally {
      window.clearTimeout(timeout);
      setIsAnalyzing(false);
    }
  }

  function handleSuggestion(prompt: string) {
    const normalized = prompt.toLowerCase();
    if (normalized.includes("demo workspace")) return loadDemo();
    if (normalized.includes("add my career profile") || normalized.includes("add a profile")) return openEditor("profile");
    if (normalized.includes("add a target role")) return openEditor("role");
    if (normalized.includes("fit check")) return void runAnalysis();
    void sendChat(prompt);
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void sendChat(draft);
  }

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void sendChat(draft);
    }
  }

  if (isCheckingSession || !authUser) {
    return (
      <main className="auth-shell auth-loading" aria-live="polite">
        <div className="loading-orbit" aria-hidden="true"><span /></div>
        <p>{isCheckingSession ? "Opening your local workspace…" : "CareerPilot could not open its local workspace."}</p>
      </main>
    );
  }

  if (!authUser.active_provider) {
    return <main className="auth-shell auth-loading" aria-live="polite"><div className="loading-orbit" aria-hidden="true"><span /></div><p>Opening your AI connection settings…</p></main>;
  }

  const memoryCount = Number(Boolean(candidateProfile)) + Number(Boolean(jobDescription));
  const profileSummary = uploadedFilename ?? (candidateProfile ? `${candidateProfile.length.toLocaleString()} characters of evidence` : "Add a CV or tell Pilot about your work");
  const roleSummary = jobDescription ? jobDescription.split(/[.!?]/)[0].slice(0, 92) : "Paste a role you are considering";
  const selectedModel = agentSettings?.models.find((item) => item.model === agentSettings.model) ?? null;
  const effortOptions = selectedModel?.supported_reasoning_efforts ?? [];
  const contextWindow = selectedModel?.context_window ?? null;
  const contextPercent = contextUsage && contextWindow
    ? Math.min(100, Math.round((contextUsage.inputTokens / contextWindow) * 100))
    : null;
  const limitWindows = agentSettings?.rate_limits
    ? [agentSettings.rate_limits.primary, agentSettings.rate_limits.secondary].filter(
      (item) => item !== null,
    )
    : [];

  return (
    <main className="pilot-shell">
      <input
        ref={fileInputRef}
        className="pilot-hidden-input"
        type="file"
        accept=".pdf,.docx,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain"
        onChange={(event) => { const file = event.currentTarget.files?.[0]; if (file) void handleCVUpload(file); }}
      />

      <aside className="pilot-left-rail">
        <a className="pilot-brand" href="#conversation" aria-label="CareerPilot home"><span>CP</span><strong>CareerPilot</strong></a>
        <div className="pilot-agent-card"><AgentAvatar /><div><strong>Pilot</strong><span><i /> Your career companion</span></div></div>
        <nav className="pilot-nav" aria-label="CareerPilot sections">
          <button className="is-active" type="button"><Icon name="home" /><span>Today</span></button>
          <Link href="/workspace"><Icon name="briefcase" /><span>Workspace</span></Link>
          <button type="button" disabled><Icon name="spark" /><span>Interview practice</span><small>Soon</small></button>
        </nav>
        <div className="pilot-left-bottom">
          <p><Icon name="spark" size={15} /> One thoughtful step is still progress.</p>
          <div className="pilot-account">
            <span>CP</span>
            <div><strong>Local workspace</strong><small>{authUser.provider_label}</small></div>
            <Link href="/settings" aria-label="Open settings"><Icon name="settings" size={17} /></Link>
          </div>
        </div>
      </aside>

      <section className="pilot-conversation" id="conversation">
        <header className="pilot-conversation-header">
          <div><span>Today with Pilot</span><h1>Good to see you.</h1></div>
          <div className="pilot-header-actions">
            <button type="button" onClick={loadDemo}>Load demo</button>
            <Link href="/workspace"><Icon name="briefcase" size={17} /><span>Workspace</span></Link>
            <Link href="/settings"><Icon name="settings" size={17} /><span>Settings</span></Link>
          </div>
        </header>

        <div className="pilot-thread">
          <div className="pilot-day-divider"><span>Today</span></div>
          {messages.map((message, index) => (
            <article className={`pilot-message pilot-message-${message.role}`} key={message.id}>
              {message.role === "assistant" ? <AgentAvatar small /> : null}
              <div className="pilot-message-stack">
                <span className="pilot-message-author">{message.role === "assistant" ? "Pilot" : "You"}</span>
                <div className="pilot-message-bubble">{message.content}</div>
                {message.report ? <MatchBrief report={message.report} reportId={message.id} /> : null}
                {message.role === "assistant" && index === messages.length - 1 && suggestions.length ? (
                  <div className="pilot-suggestions">
                    {suggestions.map((suggestion) => <button type="button" key={suggestion} onClick={() => handleSuggestion(suggestion)}>{suggestion}<span>→</span></button>)}
                  </div>
                ) : null}
              </div>
            </article>
          ))}

          {isAnalyzing || (isChatting && chatActivity) ? (
            <article className="pilot-message pilot-message-assistant pilot-thinking" aria-live="polite">
              <AgentAvatar small />
              <div className="pilot-message-stack"><span className="pilot-message-author">Pilot</span><div className="pilot-message-bubble"><span /><span /><span /> {isAnalyzing ? "Checking every claim against your evidence…" : chatActivity}</div></div>
            </article>
          ) : null}

          {pendingApproval ? (
            <article className="pilot-message pilot-message-assistant" aria-live="polite">
              <AgentAvatar small />
              <div className="pilot-message-stack">
                <span className="pilot-message-author">Pilot needs your approval</span>
                <ApprovalCard
                  approval={pendingApproval}
                  isResolving={isResolvingApproval}
                  onRespond={(choice) => void respondToApproval(choice)}
                />
              </div>
            </article>
          ) : null}

          {error ? <div className="pilot-error" role="alert"><strong>We hit a snag.</strong><span>{error}</span><button type="button" onClick={() => setError(null)}>Dismiss</button></div> : null}
          <div ref={conversationEndRef} />
        </div>

        <div className="pilot-composer-wrap">
          <form className="pilot-composer" onSubmit={handleSubmit}>
            <textarea value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={handleComposerKeyDown} placeholder="Ask Pilot to research, create, analyze, or plan…" rows={2} maxLength={4000} disabled={isChatting || isAnalyzing} aria-label="Message Pilot" />
            <div className="pilot-composer-actions">
              <div>
                <button type="button" onClick={() => fileInputRef.current?.click()} disabled={isParsingCV || isChatting || isAnalyzing} title="Attach your CV"><Icon name="paperclip" size={18} /><span>{isParsingCV ? "Reading CV…" : "Attach CV"}</span></button>
                <button type="button" onClick={() => openEditor("role")} disabled={isChatting || isAnalyzing} title="Add a target role"><Icon name="target" size={18} /><span>Add role</span></button>
              </div>
              <button className="pilot-send" type="submit" disabled={!draft.trim() || isChatting || isAnalyzing} aria-label="Send message"><Icon name="send" size={18} /></button>
            </div>
          </form>
          <p>Pilot can use local tools to complete tasks and asks before sensitive actions.</p>
        </div>
      </section>

      <aside className="pilot-context-rail">
        <section className="pilot-runtime-card" aria-label="Pilot model and usage">
          <div className="pilot-runtime-heading">
            <div><span>Agent runtime</span><strong>How Pilot thinks</strong></div>
            {agentSettings?.rate_limits?.plan_type ? <small>{agentSettings.rate_limits.plan_type}</small> : null}
          </div>

          {isLoadingAgentSettings && !agentSettings ? (
            <p className="pilot-runtime-loading">Reading model access and account limits…</p>
          ) : agentSettings ? (
            <>
              <label className="pilot-runtime-field">
                <span>Model</span>
                <select value={agentSettings.model} onChange={(event) => chooseAgentModel(event.target.value)} disabled={isSavingAgentSettings}>
                  {agentSettings.models.map((model) => <option key={model.model} value={model.model}>{model.display_name}</option>)}
                </select>
              </label>
              <label className="pilot-runtime-field">
                <span>Thinking effort</span>
                <select value={agentSettings.reasoning_effort} onChange={(event) => chooseReasoningEffort(event.target.value as ReasoningEffort)} disabled={isSavingAgentSettings}>
                  {effortOptions.map((effort) => <option key={effort.reasoning_effort} value={effort.reasoning_effort}>{effortLabel(effort.reasoning_effort)}</option>)}
                  {!effortOptions.some((effort) => effort.reasoning_effort === agentSettings.reasoning_effort) ? <option value={agentSettings.reasoning_effort}>{effortLabel(agentSettings.reasoning_effort)}</option> : null}
                </select>
              </label>
              {isAgentSettingsDirty ? (
                <button className="pilot-runtime-save" type="button" onClick={() => void saveAgentSettings()} disabled={isSavingAgentSettings}>
                  {isSavingAgentSettings ? "Applying…" : "Apply to Pilot"}<span>→</span>
                </button>
              ) : <p className="pilot-runtime-active"><i /> Active for the next message</p>}

              <div className="pilot-runtime-meter">
                <div><span>Context window</span><strong>{contextPercent === null ? "Ready" : `${contextPercent}%`}</strong></div>
                <div className="pilot-runtime-track"><span style={{ width: `${contextPercent ?? 0}%` }} /></div>
                <p>{contextUsage
                  ? `${contextUsage.inputTokens.toLocaleString()} input tokens${contextWindow ? ` of ${contextWindow.toLocaleString()}` : " in the latest turn"}`
                  : contextWindow ? `${contextWindow.toLocaleString()} tokens available` : "Usage appears after Pilot replies"}</p>
              </div>

              {limitWindows.length ? (
                <div className="pilot-limit-list">
                  {limitWindows.map((window, index) => (
                    <div className="pilot-limit-row" key={`${window.window_duration_minutes}-${index}`}>
                      <div><span>{limitWindowLabel(window.window_duration_minutes)}</span><strong>{window.used_percent}% used</strong></div>
                      <div className="pilot-runtime-track"><span style={{ width: `${Math.min(100, window.used_percent)}%` }} /></div>
                      <small>{resetLabel(window.resets_at)}</small>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="pilot-runtime-note">{agentSettings.provider === "openai-codex" ? "No account limit window was reported." : "API usage limits are managed in your OpenAI account."}</p>
              )}

              {agentSettings.rate_limits?.individual ? (
                <p className="pilot-individual-limit">
                  <span>Workspace limit</span>
                  <strong>{agentSettings.rate_limits.individual.used} of {agentSettings.rate_limits.individual.limit} used</strong>
                  <small>{agentSettings.rate_limits.individual.remaining_percent}% remaining · {resetLabel(agentSettings.rate_limits.individual.resets_at)}</small>
                </p>
              ) : null}
              {agentSettings.rate_limits?.credits?.has_credits ? (
                <p className="pilot-credit-balance"><span>Credits</span><strong>{agentSettings.rate_limits.credits.unlimited ? "Unlimited" : agentSettings.rate_limits.credits.balance ?? "Available"}</strong></p>
              ) : null}
              {agentSettings.rate_limits?.reached_type ? <p className="pilot-runtime-warning">This account’s current Codex usage limit has been reached.</p> : null}

              {agentSettings.account_usage?.lifetime_tokens !== null && agentSettings.account_usage?.lifetime_tokens !== undefined ? (
                <p className="pilot-lifetime-usage"><span>Codex activity</span><strong>{agentSettings.account_usage.lifetime_tokens.toLocaleString()} lifetime tokens</strong></p>
              ) : null}
              {agentSettings.warnings.length ? <p className="pilot-runtime-warning">{agentSettings.warnings[0]}</p> : null}
            </>
          ) : (
            <button className="pilot-runtime-retry" type="button" onClick={() => void loadAgentSettings()}>Try loading again</button>
          )}
          {agentSettingsError ? <p className="pilot-runtime-warning">{agentSettingsError}</p> : null}
        </section>

        <div className="pilot-context-heading"><div><span>Working memory</span><h2>What Pilot knows</h2></div><span>{memoryCount}/2 ready</span></div>
        <div className="pilot-memory-progress"><span style={{ width: `${memoryCount * 50}%` }} /></div>
        <p className="pilot-context-intro">This context stays with our conversation, so you don’t have to explain yourself from scratch each time.</p>

        <section className={`pilot-memory-card${candidateProfile ? " is-ready" : ""}`}>
          <div className="pilot-memory-icon"><Icon name="file" /></div>
          <div className="pilot-memory-copy"><span>Career evidence</span><strong>{candidateProfile ? "Profile ready" : "No profile yet"}</strong><p>{profileSummary}</p></div>
          <button type="button" onClick={() => openEditor("profile")}>{candidateProfile ? "Edit" : "Add"}</button>
        </section>

        <section className={`pilot-memory-card${jobDescription ? " is-ready" : ""}`}>
          <div className="pilot-memory-icon"><Icon name="target" /></div>
          <div className="pilot-memory-copy"><span>Target opportunity</span><strong>{jobDescription ? "Role ready" : "No role yet"}</strong><p>{roleSummary}</p></div>
          <button type="button" onClick={() => openEditor("role")}>{jobDescription ? "Edit" : "Add"}</button>
        </section>

        {report ? (
          <section className="pilot-latest-check"><span>Latest fit check</span><div><strong>{report.score}<small>/10</small></strong><p>{report.matched_skills.length} direct matches · {report.missing_skills.length} gaps</p></div></section>
        ) : null}

        <button className="pilot-run-check" type="button" onClick={() => void runAnalysis()} disabled={isAnalyzing || isChatting}>
          <Icon name="spark" />{isAnalyzing ? "Running the evidence check…" : "Run a grounded fit check"}<span>→</span>
        </button>

        <div className="pilot-privacy-note"><span>Session memory</span><p>Your CV and role text are held for this browser session and are not saved as a CareerPilot profile.</p></div>
        <div className="pilot-privacy-note"><span>Local-only access</span><p>No CareerPilot account or sign-in is required on this device.</p></div>
      </aside>

      {editor ? (
        <div className="pilot-modal-backdrop" role="presentation" onMouseDown={() => setEditor(null)}>
          <section className="pilot-modal" role="dialog" aria-modal="true" aria-labelledby="pilot-editor-title" onMouseDown={(event) => event.stopPropagation()}>
            <div className="pilot-modal-heading"><span className="pilot-memory-icon"><Icon name={editor === "profile" ? "file" : "target"} /></span><div><small>Add to working memory</small><h2 id="pilot-editor-title">{editor === "profile" ? "Your career evidence" : "The role we’re exploring"}</h2></div><button type="button" onClick={() => setEditor(null)} aria-label="Close">×</button></div>
            <p>{editor === "profile" ? "Paste the evidence you want Pilot to rely on—experience, projects, skills, and outcomes. You can also attach a PDF, DOCX, or TXT CV." : "Paste the full job description when possible. Pilot will separate required evidence from preferred extras."}</p>
            {editor === "profile" ? <button className="pilot-modal-upload" type="button" onClick={() => fileInputRef.current?.click()} disabled={isParsingCV}><Icon name="paperclip" />{isParsingCV ? "Reading your CV…" : "Attach a CV instead"}</button> : null}
            <textarea value={editorDraft} onChange={(event) => setEditorDraft(event.target.value)} rows={14} maxLength={50000} placeholder={editor === "profile" ? "Tell Pilot about your experience…" : "Paste the job description…"} autoFocus />
            <div className="pilot-modal-footer"><small>{editorDraft.length.toLocaleString()} / 50,000 · Editable anytime</small><div><button type="button" onClick={() => setEditor(null)}>Cancel</button><button className="pilot-modal-save" type="button" onClick={saveEditor}>Save to memory <span>→</span></button></div></div>
          </section>
        </div>
      ) : null}
    </main>
  );
}
