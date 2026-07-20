"use client";

import {
  FormEvent,
  KeyboardEvent,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { getGuidedProgress, type GuidedStepId } from "../guided-progress";
import type {
  AuthSessionResponse,
  AuthUser,
  ConversationSession,
  ConversationSessionList,
  MatchResponse,
} from "../types";
import { ApplicationSummary } from "./application-summary";
import { ApprovalHistoryPanel } from "./approval-history-panel";
import { parseApprovalHistoryPage } from "./approval-history";
import { API_BASE_URL, apiRequest, openArtifact } from "./api";
import { FormPreviewPanel } from "./form-preview-panel";
import { reconcileFormPreviews } from "./form-preview";
import type {
  Application,
  CandidateProfile,
  CompanionSettings,
  FormPreview,
  FormPreviewFieldSpec,
  Job,
  ModelRoute,
  ProfileClaim,
  ProfileProject,
  ProjectAnalysis,
  Revision,
} from "./types";

type WorkspaceTab =
  | "overview"
  | "profile"
  | "jobs"
  | "applications"
  | "controls";

type DateFilter = "any" | "today" | "7d" | "30d";

interface CsvImportResult {
  created_jobs: number;
  updated_jobs: number;
  created_applications: number;
  updated_applications: number;
  skipped: number;
  errors: string[];
}

const WORKPLACE_OPTIONS = [
  ["any", "Any workplace"],
  ["remote", "Remote"],
  ["hybrid", "Hybrid"],
  ["onsite", "On-site"],
  ["unknown", "Not specified"],
] as const;

const COMPANY_SIZE_OPTIONS = [
  ["any", "Any company size"],
  ["1-10", "1–10 employees"],
  ["11-50", "11–50 employees"],
  ["51-200", "51–200 employees"],
  ["201-500", "201–500 employees"],
  ["501-1000", "501–1,000 employees"],
  ["1001-5000", "1,001–5,000 employees"],
  ["5001-10000", "5,001–10,000 employees"],
  ["10001+", "10,001+ employees"],
  ["unknown", "Not specified"],
] as const;

const SENIORITY_OPTIONS = [
  ["", "Choose a level"],
  ["intern", "Intern"],
  ["entry", "Entry level"],
  ["junior", "Junior"],
  ["mid", "Mid-level"],
  ["senior", "Senior"],
  ["lead", "Lead"],
  ["staff", "Staff"],
  ["principal", "Principal"],
  ["manager", "Engineering / people manager"],
  ["director", "Director"],
  ["executive", "Executive"],
  ["flexible", "Flexible / depends on role"],
] as const;

const LANGUAGE_PROFICIENCY_GROUPS = [
  {
    label: "Native proficiency",
    options: ["Native / bilingual"],
  },
  {
    label: "CEFR standard",
    options: ["CEFR A1", "CEFR A2", "CEFR B1", "CEFR B2", "CEFR C1", "CEFR C2"],
  },
  {
    label: "Working proficiency",
    options: [
      "Elementary proficiency",
      "Limited working proficiency",
      "Professional working proficiency",
      "Full professional proficiency",
    ],
  },
] as const;

const APPLICATION_STATUS_OPTIONS = [
  ["discovered", "Tracked"],
  ["scored", "Fit reviewed"],
  ["approved", "Approved to tailor"],
  ["tailoring", "Tailoring"],
  ["ready", "Ready to apply"],
  ["form_previewed", "Form preview ready"],
  ["form_filled", "Form filled"],
  ["submitted", "Applied"],
  ["followed_up", "Followed up"],
  ["oa", "Online assessment"],
  ["oa_failed", "OA failed"],
  ["interview", "Interview (legacy)"],
  ["interview_1", "Interview 1"],
  ["interview_1_failed", "Interview 1 failed"],
  ["interview_2", "Interview 2"],
  ["interview_2_failed", "Interview 2 failed"],
  ["final_interview", "Final interview"],
  ["final_interview_failed", "Final interview failed"],
  ["offer", "Offer"],
  ["accepted", "Accepted"],
  ["rejected", "Rejected"],
  ["no_response", "No response"],
  ["withdrawn", "Withdrawn"],
] as const;

const APPLICATION_STARTED_STATUSES = new Set([
  "submitted",
  "followed_up",
  "oa",
  "oa_failed",
  "interview",
  "interview_1",
  "interview_1_failed",
  "interview_2",
  "interview_2_failed",
  "final_interview",
  "final_interview_failed",
  "offer",
  "accepted",
  "rejected",
  "no_response",
]);

function formatDate(value?: string): string {
  if (!value) return "Not specified";
  const date = new Date(value.length === 10 ? `${value}T00:00:00` : value);
  if (Number.isNaN(date.getTime())) return "Not specified";
  return new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" }).format(date);
}

function matchesDateFilter(value: string | undefined, filter: DateFilter): boolean {
  if (filter === "any") return true;
  if (!value) return false;
  const date = new Date(value.length === 10 ? `${value}T00:00:00` : value);
  if (Number.isNaN(date.getTime())) return false;
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const threshold = new Date(start);
  if (filter === "7d") threshold.setDate(threshold.getDate() - 6);
  if (filter === "30d") threshold.setDate(threshold.getDate() - 29);
  return date >= threshold;
}

function applicationStatusLabel(status: string): string {
  return APPLICATION_STATUS_OPTIONS.find(([value]) => value === status)?.[1]
    ?? status.replaceAll("_", " ");
}

function companySizeLabel(size?: string): string {
  return COMPANY_SIZE_OPTIONS.find(([value]) => value === (size ?? "unknown"))?.[1]
    ?? "Not specified";
}

const EMPTY_PROFILE: CandidateProfile = {
  display_name: "",
  seniority: "",
  email: "",
  phone: "",
  claims: [],
  target_roles: [],
  preferred_locations: [],
  languages: [],
  projects: [],
  work_authorization: [],
  source_documents: [],
};

const CLAIM_TYPES = [
  ["achievement", "Achievement"],
  ["experience", "Experience"],
  ["project", "Project"],
  ["skill", "Skill"],
  ["degree", "Education"],
  ["publication", "Publication"],
  ["certification", "Certification"],
  ["language", "Language"],
  ["other", "Other"],
] as const;

const LANGUAGE_NAME = "(?:English|German|Chinese|French|Spanish|Italian|Portuguese|Dutch|Arabic|Japanese|Korean)";
const LANGUAGE_LIST_PATTERN = new RegExp(
  `^${LANGUAGE_NAME}(?:\\s*\\([^)]*\\))?(?:\\s*[,;|/]\\s*${LANGUAGE_NAME}(?:\\s*\\([^)]*\\))?)*$`,
  "i",
);
const PUBLICATION_HINT_PATTERN = /\b(?:dataset|llm|co-?author|publication|paper|proceedings|journal|arxiv|acl|coling|emnlp|naacl|eacl|neurips|icml|iclr|aaai|ijcai|sigir|kdd|ieee|acm)\b/i;

function normalizeProfile(profile: CandidateProfile): CandidateProfile {
  const legacyEmail = profile.claims.find((claim) => claim.key === "email")?.value ?? "";
  const legacyPhone = profile.claims.find((claim) => claim.key === "phone")?.value ?? "";
  const languageClaims = profile.claims.filter(
    (claim) => claim.key === "language" && LANGUAGE_LIST_PATTERN.test(claim.value.trim()),
  );
  const importedLanguages = languageClaims.flatMap((claim) =>
    claim.value.split(/[,;|/]/).map((value) => value.trim()).filter(Boolean),
  );
  return {
    ...EMPTY_PROFILE,
    ...profile,
    email: profile.email || legacyEmail,
    phone: profile.phone || legacyPhone,
    seniority: profile.seniority || "",
    languages: Array.from(new Set([...(profile.languages ?? []), ...importedLanguages])),
    projects: (profile.projects ?? []).map((project, index) => ({
      ...project,
      id: project.id || `project-${index + 1}`,
      name: project.name || "",
      description: project.description || "",
      local_path: project.local_path || "",
      technologies: project.technologies ?? [],
      highlights: project.highlights ?? [],
    })),
    claims: profile.claims
      .filter((claim) => !["email", "phone"].includes(claim.key) && !languageClaims.includes(claim))
      .map((claim) => claim.key === "language" && PUBLICATION_HINT_PATTERN.test(claim.value)
        ? { ...claim, key: "publication" }
        : claim),
  };
}

const TAB_LABELS: Record<WorkspaceTab, string> = {
  overview: "Overview",
  profile: "My profile",
  jobs: "Job queue",
  applications: "Applications",
  controls: "Controls & memory",
};

export default function WorkspacePage() {
  const router = useRouter();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [activeTab, setActiveTab] = useState<WorkspaceTab>("overview");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [profile, setProfile] = useState<CandidateProfile>(EMPTY_PROFILE);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [applications, setApplications] = useState<Application[]>([]);
  const [routes, setRoutes] = useState<ModelRoute[]>([]);
  const [revisions, setRevisions] = useState<Revision[]>([]);
  const [pendingApprovalCount, setPendingApprovalCount] = useState(0);
  const [approvalHistoryAsOf, setApprovalHistoryAsOf] = useState("");
  const [formPreviews, setFormPreviews] = useState<Record<string, FormPreview>>({});
  const [settings, setSettings] = useState<CompanionSettings | null>(null);
  const [activeGroundedFit, setActiveGroundedFit] = useState<MatchResponse | null>(null);

  const refresh = useCallback(async (quiet = false) => {
    if (!quiet) setRefreshing(true);
    try {
      const activeConversationRequest = apiRequest<ConversationSessionList>(
        "/companion/sessions",
      ).then((sessionList) => apiRequest<ConversationSession>(
        `/companion/sessions/${encodeURIComponent(sessionList.active_session_id)}`,
      ));
      const [jobRows, applicationRows, profileRow, routeRows, revisionRows, approvalHistoryRow, previewRows, settingsRow, activeConversation] =
        await Promise.all([
          apiRequest<Job[]>("/api/v1/jobs"),
          apiRequest<Application[]>("/api/v1/applications"),
          apiRequest<CandidateProfile | null>("/api/v1/onboarding/profile"),
          apiRequest<ModelRoute[]>("/api/v1/model-routes"),
          apiRequest<Revision[]>("/api/v1/revisions"),
          apiRequest<unknown>("/api/v1/approvals/history?limit=1"),
          apiRequest<Record<string, FormPreview>>("/api/v1/applications/form-previews"),
          apiRequest<CompanionSettings>("/api/v1/settings"),
          activeConversationRequest,
        ]);
      const approvalHistory = parseApprovalHistoryPage(approvalHistoryRow);
      setJobs(jobRows);
      setApplications(applicationRows);
      setProfile(normalizeProfile(profileRow ?? EMPTY_PROFILE));
      setRoutes(routeRows);
      setRevisions(revisionRows);
      setPendingApprovalCount(approvalHistory.pendingCount);
      setApprovalHistoryAsOf(approvalHistory.asOf);
      setFormPreviews((current) => reconcileFormPreviews(current, previewRows, true));
      setSettings(settingsRow);
      setActiveGroundedFit(activeConversation.match_report);
      setError(null);
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "Your local workspace could not be refreshed.",
      );
    } finally {
      if (!quiet) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    async function start() {
      try {
        const sessionResponse = await fetch(`${API_BASE_URL}/local/session`);
        const session = sessionResponse.ok
          ? ((await sessionResponse.json()) as AuthSessionResponse)
          : null;
        if (!active) return;
        if (!session?.authenticated || !session.user) {
          router.replace("/");
          return;
        }
        if (!session.user.active_provider) {
          router.replace("/settings");
          return;
        }
        setUser(session.user);
        await refresh(true);
      } catch {
        if (active) router.replace("/");
      } finally {
        if (active) setLoading(false);
      }
    }
    void start();
    return () => {
      active = false;
    };
  }, [refresh, router]);

  const jobById = useMemo(
    () => new Map(jobs.map((job) => [job.id, job])),
    [jobs],
  );

  if (loading || !user) {
    return (
      <main className="auth-shell auth-loading" aria-live="polite">
        <div className="loading-orbit" aria-hidden="true"><span /></div>
        <p>Opening your private career workspace…</p>
      </main>
    );
  }

  return (
    <main className="workspace-shell">
      <aside className="workspace-sidebar">
        <Link className="workspace-brand" href="/">
          <span>CP</span>
          <div><strong>CareerPilot</strong><small>Private workspace</small></div>
        </Link>
        <nav aria-label="Career workspace">
          {(Object.keys(TAB_LABELS) as WorkspaceTab[]).map((tab) => (
            <button
              className={activeTab === tab ? "is-active" : ""}
              key={tab}
              onClick={() => setActiveTab(tab)}
              type="button"
            >
              {TAB_LABELS[tab]}
              {tab === "applications" && applications.length ? (
                <span>{applications.length}</span>
              ) : null}
            </button>
          ))}
        </nav>
        <div className="workspace-local-note">
          <i />
          <div><strong>Stored in your runtime</strong><span>Account-isolated workspace</span></div>
        </div>
        <div className="workspace-sidebar-bottom">
          <Link href="/">Talk with Pilot</Link>
          <Link href="/settings">AI connection</Link>
        </div>
      </aside>

      <section className="workspace-main">
        <header className="workspace-topbar">
          <div>
            <span>{TAB_LABELS[activeTab]}</span>
            <strong>Local workspace</strong>
          </div>
          <button
            disabled={refreshing}
            onClick={() => void refresh()}
            type="button"
          >
            {refreshing ? "Refreshing…" : "Refresh workspace"}
          </button>
        </header>

        {error ? (
          <div className="workspace-alert" role="alert">
            <div><strong>Something needs attention.</strong><span>{error}</span></div>
            <button onClick={() => setError(null)} type="button">Dismiss</button>
          </div>
        ) : null}

        <div className="workspace-content">
          {activeTab === "overview" ? (
            <Overview
              activeGroundedFit={activeGroundedFit}
              applications={applications}
              jobs={jobs}
              pendingApprovalCount={pendingApprovalCount}
              profile={profile}
              settings={settings}
              setTab={setActiveTab}
              user={user}
            />
          ) : null}
          {activeTab === "profile" ? (
            <ProfilePanel
              profile={profile}
              refresh={refresh}
              setError={setError}
              setProfile={setProfile}
            />
          ) : null}
          {activeTab === "jobs" ? (
            <JobsPanel
              applications={applications}
              jobs={jobs}
              refresh={refresh}
              setError={setError}
            />
          ) : null}
          {activeTab === "applications" ? (
            <ApplicationsPanel
              applications={applications}
              formPreviews={formPreviews}
              jobById={jobById}
              refresh={refresh}
              setError={setError}
              setFormPreviews={setFormPreviews}
            />
          ) : null}
          {activeTab === "controls" ? (
            <ControlsPanel
              key={routes.map((route) => `${route.name}:${route.provider}:${route.model}:${route.reasoning_effort}:${route.cost_budget_usd}`).join("|")}
              accountId={user.id}
              approvalRefreshKey={approvalHistoryAsOf}
              refresh={refresh}
              revisions={revisions}
              routes={routes}
              setError={setError}
              settings={settings}
            />
          ) : null}
        </div>
      </section>
    </main>
  );
}

function Overview({
  activeGroundedFit,
  applications,
  jobs,
  pendingApprovalCount,
  profile,
  settings,
  setTab,
  user,
}: {
  activeGroundedFit: MatchResponse | null;
  applications: Application[];
  jobs: Job[];
  pendingApprovalCount: number;
  profile: CandidateProfile;
  settings: CompanionSettings | null;
  setTab: (tab: WorkspaceTab) => void;
  user: AuthUser;
}) {
  const activeApplications = applications.filter(
    (item) => !["offer", "rejected", "withdrawn"].includes(item.status),
  );
  const verifiedClaims = [...profile.claims, ...profile.work_authorization].filter(
    (claim) => claim.status === "verified",
  ).length;
  const ranked = jobs.filter((job) => typeof job.score === "number");
  const bestJob = [...ranked].sort((a, b) => (b.score ?? 0) - (a.score ?? 0))[0];
  const progress = getGuidedProgress({
    aiConnected: Boolean(user.active_provider),
    verifiedProfileFacts: verifiedClaims,
    targetOpportunities: jobs.length,
    hasGroundedFit: Boolean(activeGroundedFit),
  });

  const stepDetail: Record<GuidedStepId, string> = {
    "ai-connection": user.provider_label
      ? `${user.provider_label} is active.`
      : "Choose an OpenAI connection for Pilot.",
    "reviewed-profile": verifiedClaims
      ? `${verifiedClaims} confirmed ${verifiedClaims === 1 ? "fact is" : "facts are"} ready to ground your work.`
      : "Import your CV, check the extracted details, and confirm at least one fact.",
    "target-opportunity": jobs.length
      ? `${jobs.length} ${jobs.length === 1 ? "opportunity is" : "opportunities are"} saved in your queue.`
      : "Save one real role so your next decision has a concrete target.",
    "grounded-fit": activeGroundedFit
      ? `${activeGroundedFit.score}/10 evidence-grounded fit is saved in your active Pilot session.`
      : "Compare a role with your confirmed evidence in Pilot for a separate 0–10 fit score.",
  };

  const nextAction = progress.nextStep ?? "grounded-fit";
  const nextCopy: Record<GuidedStepId, { title: string; body: string; label: string }> = {
    "ai-connection": {
      title: "Connect Pilot to an AI provider",
      body: "Choose the connection Pilot should use before adding career data.",
      label: "Open AI connection",
    },
    "reviewed-profile": {
      title: "Review the facts Pilot can use",
      body: "Import your CV, correct the extraction, and explicitly confirm only supported facts.",
      label: "Review my profile",
    },
    "target-opportunity": {
      title: "Add your first real opportunity",
      body: "Save a job description to create a deterministic queue priority and a target for deeper review.",
      label: "Add an opportunity",
    },
    "grounded-fit": progress.nextStep
      ? {
          title: bestJob
            ? `Ground the fit for ${bestJob.title}`
            : "Run your first grounded fit",
          body: typeof bestJob?.score === "number"
            ? `Its ${bestJob.score}/100 queue priority only orders opportunities. Open Pilot to compare the role with your evidence.`
            : "Open Pilot to compare a target role with your evidence and surface supported matches and gaps.",
          label: "Open grounded fit",
        }
      : {
          title: "Turn the evidence into a next move",
          body: `${activeGroundedFit?.score ?? "Your"}/10 grounded fit is ready. Continue with Pilot to work through the first supported gap or strength.`,
          label: "Continue with Pilot",
        },
  };

  function performNextAction() {
    if (nextAction === "reviewed-profile") setTab("profile");
    if (nextAction === "target-opportunity") setTab("jobs");
  }

  return (
    <section>
      <div className="workspace-hero">
        <span className="workspace-eyebrow">YOUR OPERATING PICTURE</span>
        <h1>Make the next move count.</h1>
        <p>
          Pilot keeps the evidence honest, the queue focused, and every external
          action under your control.
        </p>
      </div>
      <div className="workspace-metrics">
        <Metric label="Confirmed career facts" value={verifiedClaims} />
        <Metric label="Jobs in your queue" value={jobs.length} />
        <Metric label="Applications moving" value={activeApplications.length} />
        <Metric label="AI cost today" value={`$${Number(settings?.daily_cost_usd ?? 0).toFixed(2)}`} />
      </div>
      <div className="workspace-guided-grid">
        <article className="workspace-card workspace-guided-card" aria-labelledby="guided-start-title">
          <div className="workspace-guided-heading">
            <div>
              <span className="workspace-kicker">GUIDED START</span>
              <h2 id="guided-start-title">Build one grounded decision.</h2>
            </div>
            <strong aria-label={`${progress.completedCount} of 4 steps complete`}>
              {progress.completedCount}<small>/4</small>
            </strong>
          </div>
          <ol className="workspace-checklist">
            {progress.steps.map((step, index) => (
              <li
                aria-current={step.current ? "step" : undefined}
                className={`${step.complete ? "is-complete" : ""}${step.current ? " is-current" : ""}`}
                key={step.id}
              >
                <span aria-hidden="true">{step.complete ? "✓" : index + 1}</span>
                <div><strong>{step.label}</strong><p>{stepDetail[step.id]}</p></div>
                <small>{step.complete ? "Done" : step.current ? "Next" : "Waiting"}</small>
              </li>
            ))}
          </ol>
        </article>
        <div className="workspace-guided-side">
          <article className="workspace-card workspace-card-accent workspace-next-action">
            <span className="workspace-kicker">NEXT BEST STEP</span>
            <h2>{nextCopy[nextAction].title}</h2>
            <p>{nextCopy[nextAction].body}</p>
            {nextAction === "ai-connection" ? (
              <Link href="/settings">{nextCopy[nextAction].label}</Link>
            ) : nextAction === "grounded-fit" ? (
              <Link href="/">{nextCopy[nextAction].label}</Link>
            ) : (
              <button onClick={performNextAction} type="button">{nextCopy[nextAction].label}</button>
            )}
          </article>
          <article className="workspace-card workspace-score-guide" aria-labelledby="score-guide-title">
            <span className="workspace-kicker">TWO DIFFERENT SIGNALS</span>
            <h2 id="score-guide-title">Priority is not fit.</h2>
            <dl>
              <div><dt><strong>0–100</strong> Queue priority</dt><dd>Fixed, deterministic rules order jobs in your queue. This does not measure your personal fit.</dd></div>
              <div><dt><strong>0–10</strong> Grounded fit</dt><dd>Pilot compares a role with your evidence, then shows supported matches, gaps, and cautions.</dd></div>
            </dl>
          </article>
        </div>
      </div>
      {pendingApprovalCount ? (
        <div className="workspace-notice">
          <strong>{pendingApprovalCount} action{pendingApprovalCount === 1 ? "" : "s"} waiting for approval</strong>
          <span>Nothing external proceeds just because a model suggested it.</span>
          <button onClick={() => setTab("controls")} type="button">Review</button>
        </div>
      ) : null}
    </section>
  );
}

function Metric({ label, value }: { label: string; value: number | string }) {
  return <div><strong>{value}</strong><span>{label}</span></div>;
}

function CsvImportPanel({
  endpoint,
  kind,
  refresh,
  setError,
}: {
  endpoint: string;
  kind: "jobs" | "applications";
  refresh: (quiet?: boolean) => Promise<void>;
  setError: (message: string | null) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function importCsv(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    setBusy(true);
    setMessage(null);
    try {
      const result = await apiRequest<CsvImportResult>(endpoint, {
        method: "POST",
        body: new FormData(formElement),
      });
      const created = kind === "jobs" ? result.created_jobs : result.created_applications;
      const updated = kind === "jobs" ? result.updated_jobs : result.updated_applications;
      const errorSummary = result.errors.length
        ? ` ${result.errors.length} row${result.errors.length === 1 ? "" : "s"} need attention: ${result.errors[0]}`
        : "";
      setMessage(`Imported ${created} and updated ${updated} ${kind}.${errorSummary}`);
      formElement.reset();
      await refresh(true);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : `The ${kind} CSV could not be imported.`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="workspace-csv-import" onSubmit={importCsv}>
      <div>
        <strong>Import {kind} from CSV</strong>
        <span>UTF-8 CSV, up to 1,000 rows. Existing records are updated by job URL.</span>
        {message ? <small role="status">{message}</small> : null}
      </div>
      <input accept=".csv,text/csv" aria-label={`Choose ${kind} CSV`} name="file" required type="file" />
      <button disabled={busy} type="submit">{busy ? "Importing…" : "Import CSV"}</button>
    </form>
  );
}

function ProfilePanel({
  profile,
  refresh,
  setError,
  setProfile,
}: {
  profile: CandidateProfile;
  refresh: (quiet?: boolean) => Promise<void>;
  setError: (message: string | null) => void;
  setProfile: (profile: CandidateProfile) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [analyzingProjectId, setAnalyzingProjectId] = useState<string | null>(null);

  async function importCV(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    try {
      const result = await apiRequest<{ profile: CandidateProfile }>(
        "/api/v1/onboarding/import",
        { method: "POST", body: form },
      );
      setProfile(normalizeProfile(result.profile));
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The CV could not be imported.");
    } finally {
      setBusy(false);
    }
  }

  async function saveProfile() {
    setBusy(true);
    try {
      await apiRequest("/api/v1/onboarding/profile", {
        method: "PUT",
        body: JSON.stringify(profile),
      });
      await refresh(true);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The profile could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  async function analyzeProfileProject(project: ProfileProject, index: number) {
    setAnalyzingProjectId(project.id);
    try {
      const analysis = await apiRequest<ProjectAnalysis>(
        "/api/v1/onboarding/projects/analyze",
        { method: "POST", body: JSON.stringify(project) },
      );
      const projects = [...profile.projects];
      const previouslyDetected = new Set([
        ...(project.analysis?.technologies ?? []),
        ...(project.analysis?.primary_languages ?? []),
      ]);
      projects[index] = {
        ...project,
        analysis,
        technologies: Array.from(new Set([
          ...project.technologies.filter((technology) => !previouslyDetected.has(technology)),
          ...analysis.technologies,
        ])),
      };
      setProfile({ ...profile, projects });
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The repository could not be analyzed.");
    } finally {
      setAnalyzingProjectId(null);
    }
  }

  return (
    <section>
      <div className="workspace-section-heading">
        <div><span className="workspace-eyebrow">YOUR PROFILE</span><h1>Review your career facts.</h1><p>Fix anything the import got wrong, confirm facts you want Pilot to use, and remove noise.</p></div>
        <button disabled={busy} onClick={() => void saveProfile()} type="button">
          {busy ? "Saving…" : "Save profile changes"}
        </button>
      </div>
      <div className="workspace-grid-two">
        <form className="workspace-card workspace-import-card" onSubmit={importCV}>
          <span className="workspace-kicker">IMPORT</span>
          <h2>Import details from a CV</h2>
          <p>PDF or DOCX, up to 20 MB. The original and extracted text stay in your private workspace.</p>
          <input accept=".pdf,.docx" name="file" required type="file" />
          <button disabled={busy} type="submit">{busy ? "Reading…" : "Import details"}</button>
        </form>
        <div className="workspace-card workspace-form-grid">
          <label>Your name<input value={profile.display_name} onChange={(event) => setProfile({ ...profile, display_name: event.target.value })} /></label>
          <label>Seniority<select value={profile.seniority} onChange={(event) => setProfile({ ...profile, seniority: event.target.value })}>{SENIORITY_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label>Email<input inputMode="email" value={profile.email} onChange={(event) => setProfile({ ...profile, email: event.target.value })} /></label>
          <label>Phone<input inputMode="tel" value={profile.phone} onChange={(event) => setProfile({ ...profile, phone: event.target.value })} /></label>
          <MultiValueEditor
            items={profile.target_roles}
            label="Target roles"
            onChange={(target_roles) => setProfile({ ...profile, target_roles })}
            placeholder="AI Engineer"
          />
          <MultiValueEditor
            items={profile.preferred_locations}
            label="Preferred locations"
            onChange={(preferred_locations) => setProfile({ ...profile, preferred_locations })}
            placeholder="Berlin, Germany"
          />
          <LanguageEditor
            languages={profile.languages}
            onChange={(languages) => setProfile({ ...profile, languages })}
          />
        </div>
      </div>
      <ProjectsEditor
        analyzingProjectId={analyzingProjectId}
        onAnalyze={(project, index) => void analyzeProfileProject(project, index)}
        onChange={(projects) => setProfile({ ...profile, projects })}
        projects={profile.projects}
      />
      <div className="workspace-card workspace-claims-card">
        <div className="workspace-card-heading">
          <div>
            <h2>Career facts to review</h2>
            <p>Confirmed facts can be used in fit checks and tailored applications. Everything else stays out.</p>
          </div>
          <span>{profile.claims.filter((claim) => claim.status === "verified").length} confirmed</span>
        </div>
        {profile.claims.length ? (
          <div className="workspace-claim-list">
            {profile.claims.map((claim, index) => (
              <ClaimReviewRow
                claim={claim}
                key={`${claim.key}-${claim.value}-${index}`}
                onChange={(updatedClaim) => {
                  const claims = [...profile.claims];
                  claims[index] = updatedClaim;
                  setProfile({ ...profile, claims });
                }}
                onRemove={() => {
                  setProfile({
                    ...profile,
                    claims: profile.claims.filter((_, claimIndex) => claimIndex !== index),
                  });
                }}
              />
            ))}
          </div>
        ) : <div className="workspace-empty"><strong>No career facts waiting for review.</strong><span>Import a PDF or DOCX CV above to get started.</span></div>}
      </div>
    </section>
  );
}

function MultiValueEditor({
  className = "",
  items,
  label,
  onChange,
  placeholder,
}: {
  className?: string;
  items: string[];
  label: string;
  onChange: (items: string[]) => void;
  placeholder: string;
}) {
  const [draft, setDraft] = useState("");

  function addItem() {
    const value = draft.trim();
    if (!value) return;
    if (!items.some((item) => item.toLocaleLowerCase() === value.toLocaleLowerCase())) {
      onChange([...items, value]);
    }
    setDraft("");
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key !== "Enter") return;
    event.preventDefault();
    addItem();
  }

  return (
    <div className={`workspace-field ${className}`.trim()}>
      <span>{label}</span>
      {items.length ? (
        <div className="workspace-chip-list">
          {items.map((item) => (
            <span key={item}>
              {item}
              <button
                aria-label={`Remove ${item}`}
                onClick={() => onChange(items.filter((value) => value !== item))}
                type="button"
              >×</button>
            </span>
          ))}
        </div>
      ) : null}
      <div className="workspace-inline-add">
        <input
          aria-label={label}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          value={draft}
        />
        <button disabled={!draft.trim()} onClick={addItem} type="button">Add</button>
      </div>
      <small>Type the full value, including spaces, then press Enter or Add.</small>
    </div>
  );
}

function LanguageEditor({
  languages,
  onChange,
}: {
  languages: string[];
  onChange: (languages: string[]) => void;
}) {
  const [name, setName] = useState("");
  const [proficiency, setProficiency] = useState("Professional working proficiency");

  function addLanguage() {
    const languageName = name.trim();
    if (!languageName) return;
    const value = `${languageName} (${proficiency})`;
    const normalizedName = languageName.toLocaleLowerCase();
    onChange([
      ...languages.filter(
        (language) => languageNameFromValue(language).toLocaleLowerCase() !== normalizedName,
      ),
      value,
    ]);
    setName("");
  }

  return (
    <div className="workspace-field workspace-wide">
      <span>Languages</span>
      {languages.length ? (
        <div className="workspace-chip-list">
          {languages.map((language) => (
            <span key={language}>
              {language}
              <button
                aria-label={`Remove ${language}`}
                onClick={() => onChange(languages.filter((value) => value !== language))}
                type="button"
              >×</button>
            </span>
          ))}
        </div>
      ) : null}
      <div className="workspace-language-add">
        <input
          aria-label="Language name"
          onChange={(event) => setName(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              addLanguage();
            }
          }}
          placeholder="Language, e.g. Chinese"
          value={name}
        />
        <select
          aria-label="Language proficiency"
          onChange={(event) => setProficiency(event.target.value)}
          value={proficiency}
        >
          {LANGUAGE_PROFICIENCY_GROUPS.map((group) => (
            <optgroup key={group.label} label={group.label}>
              {group.options.map((option) => <option key={option}>{option}</option>)}
            </optgroup>
          ))}
        </select>
        <button disabled={!name.trim()} onClick={addLanguage} type="button">Add</button>
      </div>
      <small>Choose a CEFR or working-proficiency standard so Pilot can compare role requirements consistently.</small>
    </div>
  );
}

function languageNameFromValue(value: string): string {
  return value.replace(/\s*\([^)]*\)\s*$/, "").trim();
}

function ProjectsEditor({
  analyzingProjectId,
  onAnalyze,
  onChange,
  projects,
}: {
  analyzingProjectId: string | null;
  onAnalyze: (project: ProfileProject, index: number) => void;
  onChange: (projects: ProfileProject[]) => void;
  projects: ProfileProject[];
}) {
  function addProject() {
    onChange([
      ...projects,
      {
        id: globalThis.crypto?.randomUUID?.() ?? `project-${Date.now()}`,
        name: "",
        description: "",
        local_path: "",
        technologies: [],
        highlights: [],
      },
    ]);
  }

  function updateProject(index: number, update: Partial<ProfileProject>) {
    const updated = [...projects];
    updated[index] = { ...updated[index], ...update };
    onChange(updated);
  }

  return (
    <div className="workspace-card workspace-projects-card">
      <div className="workspace-card-heading">
        <div>
          <h2>Projects and codebases</h2>
          <p>Link evidence to a public GitHub repository or a local checkout, then generate focused improvements and interview questions.</p>
        </div>
        <button onClick={addProject} type="button">Add project</button>
      </div>
      {projects.length ? (
        <div className="workspace-project-list">
          {projects.map((project, index) => {
            const analyzing = analyzingProjectId === project.id;
            return (
              <article className="workspace-project" key={project.id}>
                <div className="workspace-form-grid">
                  <label>Project name<input placeholder="CareerPilot" value={project.name} onChange={(event) => updateProject(index, { name: event.target.value })} /></label>
                  <label>Public GitHub repository<input inputMode="url" placeholder="https://github.com/owner/repository" value={project.repository_url ?? ""} onChange={(event) => updateProject(index, { repository_url: event.target.value || undefined, analysis: undefined })} /></label>
                  <label className="workspace-wide">What it does<textarea rows={3} placeholder="The problem, your contribution, and the outcome." value={project.description} onChange={(event) => updateProject(index, { description: event.target.value })} /></label>
                  <label className="workspace-wide">Local codebase path<input placeholder="/Users/you/code/project" value={project.local_path} onChange={(event) => updateProject(index, { local_path: event.target.value, analysis: undefined })} /></label>
                  <MultiValueEditor
                    className="workspace-wide"
                    items={project.technologies}
                    label="Technologies"
                    onChange={(technologies) => updateProject(index, { technologies })}
                    placeholder="FastAPI"
                  />
                  <MultiValueEditor
                    className="workspace-wide"
                    items={project.highlights}
                    label="Evidence highlights"
                    onChange={(highlights) => updateProject(index, { highlights })}
                    placeholder="Reduced retrieval latency by 35%"
                  />
                </div>
                <div className="workspace-project-actions">
                  <button
                    disabled={analyzing || (!project.local_path.trim() && !project.repository_url)}
                    onClick={() => onAnalyze(project, index)}
                    type="button"
                  >{analyzing ? "Analyzing…" : project.analysis ? "Analyze again" : "Analyze codebase"}</button>
                  <span>Read-only scan; repository code is never executed. Public GitHub repositories do not need a token.</span>
                  <button className="workspace-button-danger" onClick={() => onChange(projects.filter((_, projectIndex) => projectIndex !== index))} type="button">Remove project</button>
                </div>
                {project.analysis ? <ProjectAnalysisView analysis={project.analysis} /> : null}
              </article>
            );
          })}
        </div>
      ) : (
        <div className="workspace-empty workspace-project-empty">
          <strong>No projects linked yet.</strong>
          <span>Add one to turn code evidence into improvements and interview preparation.</span>
        </div>
      )}
    </div>
  );
}

function ProjectAnalysisView({ analysis }: { analysis: ProjectAnalysis }) {
  return (
    <div className="workspace-project-analysis">
      <div>
        <span className="workspace-kicker">LATEST REPOSITORY REVIEW</span>
        <p>{analysis.summary}</p>
        <div className="workspace-chip-list">
          <span>{analysis.file_count} files</span>
          {analysis.technologies.map((technology) => <span key={technology}>{technology}</span>)}
          {analysis.primary_languages.filter((language) => !analysis.technologies.includes(language)).map((language) => <span key={language}>{language}</span>)}
        </div>
      </div>
      <div className="workspace-project-analysis-grid">
        <div>
          <h3>Suggested improvements</h3>
          <ul>{analysis.improvement_suggestions.map((suggestion) => <li key={suggestion}>{suggestion}</li>)}</ul>
        </div>
        <div>
          <h3>Interview questions</h3>
          <ul>{analysis.interview_questions.map((question) => <li key={question}>{question}</li>)}</ul>
        </div>
      </div>
      {analysis.notable_files.length ? (
        <details>
          <summary>Files used to understand the repository</summary>
          <code>{analysis.notable_files.join("\n")}</code>
        </details>
      ) : null}
    </div>
  );
}

function ClaimReviewRow({
  claim,
  onChange,
  onRemove,
}: {
  claim: ProfileClaim;
  onChange: (claim: ProfileClaim) => void;
  onRemove: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draftKey, setDraftKey] = useState(claim.key);
  const [draftValue, setDraftValue] = useState(claim.value);
  const source = claim.evidence[0];
  const confirmed = claim.status === "verified";
  const knownType = CLAIM_TYPES.some(([value]) => value === draftKey);

  function cancelEditing() {
    setDraftKey(claim.key);
    setDraftValue(claim.value);
    setEditing(false);
  }

  function saveEditing() {
    const value = draftValue.trim();
    if (!value) return;
    onChange({
      ...claim,
      key: draftKey,
      value,
      status: "learning",
      user_verified_at: undefined,
    });
    setEditing(false);
  }

  return (
    <article className="workspace-claim">
      {editing ? (
        <div className="workspace-claim-editor">
          <label>
            Category
            <select value={draftKey} onChange={(event) => setDraftKey(event.target.value)}>
              {!knownType ? <option value={draftKey}>{draftKey}</option> : null}
              {CLAIM_TYPES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <label>
            Career fact
            <textarea rows={3} value={draftValue} onChange={(event) => setDraftValue(event.target.value)} />
          </label>
          <small>Editing resets confirmation so you can compare the corrected fact with its source.</small>
          <div className="workspace-claim-actions">
            <button disabled={!draftValue.trim()} onClick={saveEditing} type="button">Save correction</button>
            <button className="workspace-button-quiet" onClick={cancelEditing} type="button">Cancel</button>
          </div>
        </div>
      ) : (
        <div className="workspace-claim-copy">
          <div className="workspace-claim-labels">
            <span>{CLAIM_TYPES.find(([value]) => value === claim.key)?.[1] ?? claim.key}</span>
            <i className={confirmed ? "is-confirmed" : ""}>{confirmed ? "Confirmed" : "Review needed"}</i>
          </div>
          <p>{claim.value}</p>
          <small>{source?.source_name ?? "No source attached"}{source?.page ? `, page ${source.page}` : ""}</small>
          {source?.excerpt ? (
            <details>
              <summary>View source excerpt</summary>
              <blockquote>{source.excerpt}</blockquote>
            </details>
          ) : null}
        </div>
      )}
      {!editing ? (
        <div className="workspace-claim-actions">
          <button
            disabled={!source}
            onClick={() => onChange({
              ...claim,
              status: confirmed ? "learning" : "verified",
              user_verified_at: confirmed ? undefined : new Date().toISOString(),
            })}
            type="button"
          >
            {confirmed ? "Undo confirm" : "Confirm fact"}
          </button>
          <button className="workspace-button-quiet" onClick={() => setEditing(true)} type="button">Edit</button>
          <button className="workspace-button-danger" onClick={onRemove} type="button">Remove</button>
        </div>
      ) : null}
    </article>
  );
}

function JobsPanel({
  applications,
  jobs,
  refresh,
  setError,
}: {
  applications: Application[];
  jobs: Job[];
  refresh: (quiet?: boolean) => Promise<void>;
  setError: (message: string | null) => void;
}) {
  const [showForm, setShowForm] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [dateFilter, setDateFilter] = useState<DateFilter>("any");
  const [workplaceFilter, setWorkplaceFilter] = useState("any");
  const [companySizeFilter, setCompanySizeFilter] = useState("any");
  const trackedJobs = new Set(applications.map((item) => item.job_id));

  async function addJob(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const sourceUrl = String(form.get("url") ?? "").trim();
    const spec: Record<string, unknown> = {
      title: String(form.get("title") ?? "").trim(),
      company: String(form.get("company") ?? "").trim(),
      locations: String(form.get("location") ?? "").split(";").map((item) => item.trim()).filter(Boolean),
      description: String(form.get("description") ?? "").trim(),
      posted_date: String(form.get("posted_date") ?? "") || undefined,
      workplace_type: String(form.get("workplace_type") ?? "unknown"),
      company_size: String(form.get("company_size") ?? "unknown"),
      source_type: "manual",
    };
    if (sourceUrl) spec.source_url = sourceUrl;
    setBusyId("new");
    try {
      await apiRequest("/api/v1/jobs", {
        method: "POST",
        body: JSON.stringify({ spec, canonical_url: sourceUrl }),
      });
      formElement.reset();
      setShowForm(false);
      await refresh(true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The job could not be added.");
    } finally {
      setBusyId(null);
    }
  }

  async function act(path: string, id: string) {
    setBusyId(id);
    try {
      await apiRequest(path, { method: "POST" });
      await refresh(true);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The job could not be updated.");
    } finally {
      setBusyId(null);
    }
  }

  const filteredJobs = jobs
    .filter((job) => {
      const normalizedQuery = query.trim().toLocaleLowerCase();
      const matchesQuery = !normalizedQuery || [
        job.company,
        job.title,
        ...job.spec.locations,
      ].some((value) => value.toLocaleLowerCase().includes(normalizedQuery));
      return matchesQuery
        && matchesDateFilter(job.spec.posted_date, dateFilter)
        && (workplaceFilter === "any" || (job.spec.workplace_type ?? "unknown") === workplaceFilter)
        && (companySizeFilter === "any" || (job.spec.company_size ?? "unknown") === companySizeFilter);
    })
    .sort((a, b) => (b.score ?? -1) - (a.score ?? -1));

  function clearFilters() {
    setQuery("");
    setDateFilter("any");
    setWorkplaceFilter("any");
    setCompanySizeFilter("any");
  }

  return (
    <section>
      <div className="workspace-section-heading"><div><span className="workspace-eyebrow">FOCUSED SEARCH</span><h1>Your job queue.</h1><p>The 0–100 queue priority uses deterministic rules to order opportunities. It is not your evidence-grounded fit.</p></div><button onClick={() => setShowForm((value) => !value)} type="button">{showForm ? "Close form" : "Add a job"}</button></div>
      <CsvImportPanel endpoint="/api/v1/jobs/import" kind="jobs" refresh={refresh} setError={setError} />
      {showForm ? (
        <form className="workspace-card workspace-job-form" onSubmit={addJob}>
          <label>Company<input name="company" required /></label>
          <label>Role<input name="title" required /></label>
          <label>Locations<input name="location" placeholder="Berlin, Germany; Munich, Germany" /></label>
          <label>Job URL<input name="url" type="url" /></label>
          <label>Posted date<input name="posted_date" type="date" /></label>
          <label>Workplace<select defaultValue="unknown" name="workplace_type">{WORKPLACE_OPTIONS.filter(([value]) => value !== "any").map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label>Company size<select defaultValue="unknown" name="company_size">{COMPANY_SIZE_OPTIONS.filter(([value]) => value !== "any").map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label className="workspace-wide">Job description<textarea name="description" required rows={10} /></label>
          <button disabled={busyId === "new"} type="submit">{busyId === "new" ? "Saving…" : "Save opportunity"}</button>
        </form>
      ) : null}
      <div className="workspace-filters" aria-label="Filter jobs">
        <label className="workspace-filter-search"><span>Search</span><input onChange={(event) => setQuery(event.target.value)} placeholder="Role, company, or location" type="search" value={query} /></label>
        <label><span>Posted</span><select onChange={(event) => setDateFilter(event.target.value as DateFilter)} value={dateFilter}><option value="any">Any date</option><option value="today">Today</option><option value="7d">Past 7 days</option><option value="30d">Past 30 days</option></select></label>
        <label><span>Workplace</span><select onChange={(event) => setWorkplaceFilter(event.target.value)} value={workplaceFilter}>{WORKPLACE_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <label><span>Company size</span><select onChange={(event) => setCompanySizeFilter(event.target.value)} value={companySizeFilter}>{COMPANY_SIZE_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <div><strong>{filteredJobs.length}</strong><span>of {jobs.length} jobs</span><button onClick={clearFilters} type="button">Clear</button></div>
      </div>
      <div className="workspace-stack">
        {filteredJobs.map((job) => (
          <article className="workspace-card workspace-job-card" key={job.id}>
            <div
              aria-label={typeof job.score !== "number" ? "Queue priority not calculated" : `Queue priority ${job.score} out of 100`}
              className={`workspace-score workspace-tier-${job.tier ?? "none"}`}
            >
              <small>Priority</small>
              <strong>{job.score ?? "—"}{typeof job.score === "number" ? <small>/100</small> : null}</strong>
              <span>{job.tier ? `Tier ${job.tier}` : "Not calculated"}</span>
            </div>
            <div className="workspace-job-copy">
              <span>{job.company}</span>
              <h2>{job.title}</h2>
              <p>{job.spec.locations.join(" · ") || "Location not listed"}</p>
              <div className="workspace-job-meta">
                <span>Posted {formatDate(job.spec.posted_date)}</span>
                <span>{WORKPLACE_OPTIONS.find(([value]) => value === (job.spec.workplace_type ?? "unknown"))?.[1]}</span>
                <span>{companySizeLabel(job.spec.company_size)}</span>
                {job.canonical_url ? <a href={job.canonical_url} rel="noreferrer" target="_blank">View job ↗</a> : <span>No URL</span>}
              </div>
              {job.score_explanation.length ? <details><summary>Why this queue priority?</summary><ul>{job.score_explanation.map((reason) => <li key={reason}>{reason}</li>)}</ul></details> : null}
            </div>
            <div className="workspace-actions"><button disabled={busyId === job.id} onClick={() => void act(`/api/v1/jobs/${job.id}/score`, job.id)} type="button">{typeof job.score !== "number" ? "Calculate priority" : "Recalculate priority"}</button><button disabled={busyId === job.id || trackedJobs.has(job.id)} onClick={() => void act(`/api/v1/applications?job_id=${job.id}`, job.id)} type="button">{trackedJobs.has(job.id) ? "Tracked" : "Track application"}</button></div>
          </article>
        ))}
        {!jobs.length ? <div className="workspace-empty workspace-card"><strong>Your queue is empty.</strong><span>Add a job description to start prioritizing opportunities.</span></div> : null}
        {jobs.length && !filteredJobs.length ? <div className="workspace-empty workspace-card"><strong>No jobs match these filters.</strong><span>Clear one or more filters to see the rest of your queue.</span></div> : null}
      </div>
    </section>
  );
}

const NEXT_STATUS: Record<string, { target: string; label: string }> = {
  discovered: { target: "scored", label: "Mark fit reviewed" },
  scored: { target: "approved", label: "Approve for tailoring" },
  submitted: { target: "followed_up", label: "Record follow-up" },
};

function ApplicationsPanel({
  applications,
  formPreviews,
  jobById,
  refresh,
  setError,
  setFormPreviews,
}: {
  applications: Application[];
  formPreviews: Record<string, FormPreview>;
  jobById: Map<string, Job>;
  refresh: (quiet?: boolean) => Promise<void>;
  setError: (message: string | null) => void;
  setFormPreviews: (
    update: (current: Record<string, FormPreview>) => Record<string, FormPreview>,
  ) => void;
}) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [showSummary, setShowSummary] = useState(false);
  const [query, setQuery] = useState("");
  const [dateFilter, setDateFilter] = useState<DateFilter>("any");
  const [workplaceFilter, setWorkplaceFilter] = useState("any");
  const [companySizeFilter, setCompanySizeFilter] = useState("any");
  const [statusFilter, setStatusFilter] = useState("any");

  async function post(path: string, id: string, body?: unknown) {
    setBusyId(id);
    try {
      await apiRequest(path, {
        method: "POST",
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      await refresh(true);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The application could not be updated.");
    } finally {
      setBusyId(null);
    }
  }

  async function createFormPreview(
    application: Application,
    formReference: string,
    fields: FormPreviewFieldSpec[],
  ) {
    setBusyId(application.id);
    try {
      const preview = await apiRequest<FormPreview>(
        `/api/v1/applications/${application.id}/form-preview`,
        {
          method: "POST",
          body: JSON.stringify({ form_reference: formReference, fields }),
        },
      );
      setFormPreviews((current) => reconcileFormPreviews(current, {
        [application.id]: preview,
      }));
      await refresh(true);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The local form preview could not be created.");
    } finally {
      setBusyId(null);
    }
  }

  const filteredApplications = applications.filter((application) => {
    const job = jobById.get(application.job_id);
    const normalizedQuery = query.trim().toLocaleLowerCase();
    const matchesQuery = !normalizedQuery || [
      job?.company ?? "",
      job?.title ?? "",
      ...(job?.spec.locations ?? []),
    ].some((value) => value.toLocaleLowerCase().includes(normalizedQuery));
    return matchesQuery
      && matchesDateFilter(application.submitted_at ?? application.created_at, dateFilter)
      && (statusFilter === "any" || application.status === statusFilter)
      && (workplaceFilter === "any" || (job?.spec.workplace_type ?? "unknown") === workplaceFilter)
      && (companySizeFilter === "any" || (job?.spec.company_size ?? "unknown") === companySizeFilter);
  });

  function clearFilters() {
    setQuery("");
    setDateFilter("any");
    setWorkplaceFilter("any");
    setCompanySizeFilter("any");
    setStatusFilter("any");
  }

  function updateStatus(application: Application, status: string) {
    if (status === application.status) return;
    if (["form_previewed", "form_filled"].includes(status)) {
      setError("Form preview and form completion use dedicated workflows.");
      return;
    }
    void post(`/api/v1/applications/${application.id}/status`, application.id, {
      status,
      note: "Status updated by the user in the local workspace",
      manual_override: true,
      confirmed_by_user: APPLICATION_STARTED_STATUSES.has(status),
    });
  }

  return (
    <section>
      <div className="workspace-section-heading">
        <div><span className="workspace-eyebrow">CONTROLLED PROGRESS</span><h1>Applications.</h1><p>Pilot can prepare materials and a local field preview. You review, submit manually, and record each outcome.</p></div>
        <button aria-expanded={showSummary} onClick={() => setShowSummary((value) => !value)} type="button">{showSummary ? "Hide summary" : "View summary"}</button>
      </div>
      <CsvImportPanel endpoint="/api/v1/applications/import" kind="applications" refresh={refresh} setError={setError} />
      {showSummary ? <ApplicationSummary applications={applications} /> : null}
      <div className="workspace-filters workspace-application-filters" aria-label="Filter applications">
        <label className="workspace-filter-search"><span>Search</span><input onChange={(event) => setQuery(event.target.value)} placeholder="Role, company, or location" type="search" value={query} /></label>
        <label><span>Application date</span><select onChange={(event) => setDateFilter(event.target.value as DateFilter)} value={dateFilter}><option value="any">Any date</option><option value="today">Today</option><option value="7d">Past 7 days</option><option value="30d">Past 30 days</option></select></label>
        <label><span>Status</span><select onChange={(event) => setStatusFilter(event.target.value)} value={statusFilter}><option value="any">Any status</option>{APPLICATION_STATUS_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <label><span>Workplace</span><select onChange={(event) => setWorkplaceFilter(event.target.value)} value={workplaceFilter}>{WORKPLACE_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <label><span>Company size</span><select onChange={(event) => setCompanySizeFilter(event.target.value)} value={companySizeFilter}>{COMPANY_SIZE_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <div><strong>{filteredApplications.length}</strong><span>of {applications.length} applications</span><button onClick={clearFilters} type="button">Clear</button></div>
      </div>
      <div className="workspace-stack">
        {filteredApplications.map((application) => {
          const job = jobById.get(application.job_id);
          const next = NEXT_STATUS[application.status];
          return (
            <article className="workspace-card workspace-application" key={application.id}>
              <div className="workspace-card-heading">
                <div className="workspace-application-copy">
                  <span className={`workspace-status workspace-status-${application.status}`}>{applicationStatusLabel(application.status)}</span>
                  <h2>{job?.title ?? "Application"}</h2>
                  <p>{job?.company ?? "Unknown company"} · {application.next_action}</p>
                  <div className="workspace-job-meta">
                    <span>{application.submitted_at ? `Applied ${formatDate(application.submitted_at)}` : `Tracked ${formatDate(application.created_at)}`}</span>
                    <span>{WORKPLACE_OPTIONS.find(([value]) => value === (job?.spec.workplace_type ?? "unknown"))?.[1]}</span>
                    <span>{companySizeLabel(job?.spec.company_size)}</span>
                    {job?.canonical_url ? <a href={job.canonical_url} rel="noreferrer" target="_blank">View job ↗</a> : <span>No URL</span>}
                  </div>
                </div>
                <div className="workspace-actions">
                  <label className="workspace-status-editor">Status<select aria-label={`Status for ${job?.title ?? "application"}`} disabled={busyId === application.id} onChange={(event) => updateStatus(application, event.target.value)} value={application.status}>{APPLICATION_STATUS_OPTIONS.map(([value, label]) => <option disabled={["form_previewed", "form_filled"].includes(value) && value !== application.status} key={value} value={value}>{label}</option>)}</select></label>
                  {next ? <button disabled={busyId === application.id} onClick={() => void post(`/api/v1/applications/${application.id}/status`, application.id, { status: next.target, note: "Confirmed in local workspace" })} type="button">{next.label}</button> : null}
                  {application.status === "approved" ? <button disabled={busyId === application.id} onClick={() => void post(`/api/v1/applications/${application.id}/artifacts/generate`, application.id)} type="button">Generate application pack</button> : null}
                  {["ready", "form_previewed", "form_filled"].includes(application.status) ? <button className="workspace-danger-safe" disabled={busyId === application.id} onClick={() => void post(`/api/v1/applications/${application.id}/status`, application.id, { status: "submitted", note: "User confirmed manual submission", confirmed_by_user: true })} type="button">I submitted it manually</button> : null}
                </div>
              </div>
              {application.artifacts.length ? (
                <div className="workspace-artifacts">
                  {application.artifacts.map((artifact) => (
                    <div key={artifact.id}>
                      <button onClick={() => void openArtifact(artifact.id).catch((cause) => setError(cause instanceof Error ? cause.message : "Artifact could not be opened."))} type="button">{artifact.kind.replaceAll("_", " ")} v{artifact.version}</button>
                      <span>{artifact.approved ? "Approved" : "Needs your review"}</span>
                      {!artifact.approved ? <button disabled={busyId === artifact.id} onClick={() => void post(`/api/v1/artifacts/${artifact.id}/approve`, artifact.id, { sha256: artifact.sha256 })} type="button">Approve this exact version</button> : null}
                    </div>
                  ))}
                </div>
              ) : null}
              {["ready", "form_previewed"].includes(application.status) || formPreviews[application.id] ? (
                <FormPreviewPanel
                  application={application}
                  busy={busyId === application.id}
                  key={formPreviews[application.id]?.id ?? `empty-${application.id}`}
                  onCreate={(formReference, fields) => createFormPreview(application, formReference, fields)}
                  preview={formPreviews[application.id]}
                />
              ) : null}
            </article>
          );
        })}
        {!applications.length ? <div className="workspace-empty workspace-card"><strong>No applications tracked yet.</strong><span>Add a job, score it, then choose “Track application.”</span></div> : null}
        {applications.length && !filteredApplications.length ? <div className="workspace-empty workspace-card"><strong>No applications match these filters.</strong><span>Clear one or more filters to see the rest of your pipeline.</span></div> : null}
      </div>
    </section>
  );
}

function ControlsPanel({
  accountId,
  approvalRefreshKey,
  refresh,
  revisions,
  routes,
  setError,
  settings,
}: {
  accountId: string;
  approvalRefreshKey: string;
  refresh: (quiet?: boolean) => Promise<void>;
  revisions: Revision[];
  routes: ModelRoute[];
  setError: (message: string | null) => void;
  settings: CompanionSettings | null;
}) {
  const [drafts, setDrafts] = useState<ModelRoute[]>(() => routes.map((route) => ({ ...route })));
  const [busyId, setBusyId] = useState<string | null>(null);

  async function saveRoute(route: ModelRoute) {
    setBusyId(route.name);
    try {
      await apiRequest(`/api/v1/model-routes/${route.name}`, {
        method: "PUT",
        body: JSON.stringify(route),
      });
      await refresh(true);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The model route could not be saved.");
    } finally {
      setBusyId(null);
    }
  }

  async function revisionAction(revisionId: string, action: "rollback") {
    setBusyId(revisionId);
    try {
      await apiRequest(`/api/v1/revisions/${revisionId}/${action}`, { method: "POST" });
      await refresh(true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The revision could not be changed.");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section>
      <div className="workspace-hero"><span className="workspace-eyebrow">YOUR CONTROLS</span><h1>Models, approvals, and memory.</h1><p>Choose effort and cost by task. Every learned change remains visible and reversible.</p></div>
      <div className="workspace-budget"><div><span>Estimated API cost today</span><strong>${Number(settings?.daily_cost_usd ?? 0).toFixed(2)}</strong></div><div><span>Configured daily budget</span><strong>${Number(settings?.daily_api_budget_usd ?? 0).toFixed(2)}</strong></div><p>Subscription routes follow ChatGPT plan limits. CareerPilot never silently switches them to paid API use.</p></div>
      <div className="workspace-card">
        <div className="workspace-card-heading"><div><h2>Task model routes</h2><p>Scheduled work stays pinned and cannot use implicit fallbacks.</p></div></div>
        <div className="workspace-route-list">
          {drafts.map((route, index) => (
            <div className="workspace-route" key={route.name}>
              <div><strong>{route.name}</strong><span>{route.scheduled ? "Scheduled" : "On demand"}</span></div>
              <label>Connection<select value={route.provider} onChange={(event) => { const next = [...drafts]; next[index] = { ...route, provider: event.target.value as ModelRoute["provider"] }; setDrafts(next); }}><option value="openai-codex">ChatGPT / Codex</option><option value="openai-api">OpenAI API</option></select></label>
              <label>Model<input value={route.model} onChange={(event) => { const next = [...drafts]; next[index] = { ...route, model: event.target.value }; setDrafts(next); }} /></label>
              <label>Effort<select value={route.reasoning_effort} onChange={(event) => { const next = [...drafts]; next[index] = { ...route, reasoning_effort: event.target.value as ModelRoute["reasoning_effort"] }; setDrafts(next); }}><option value="none">None</option><option value="minimal">Minimal</option><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option><option value="xhigh">Extra high</option></select></label>
              <label>Per-run budget<input min="0" step="0.01" type="number" value={route.cost_budget_usd} onChange={(event) => { const next = [...drafts]; next[index] = { ...route, cost_budget_usd: Number(event.target.value) }; setDrafts(next); }} /></label>
              <button disabled={busyId === route.name} onClick={() => void saveRoute(route)} type="button">{busyId === route.name ? "Saving…" : "Save"}</button>
            </div>
          ))}
        </div>
      </div>
      <div className="workspace-grid-two workspace-control-grid">
        <ApprovalHistoryPanel accountId={accountId} refreshKey={approvalRefreshKey} />
        <div className="workspace-card"><div className="workspace-card-heading"><div><h2>Evolution history</h2><p>Only memory, user-owned skills, and rubrics can evolve.</p></div></div><div className="workspace-history">{revisions.slice().reverse().map((revision) => <div key={revision.id}><div><strong>{revision.kind}: {revision.name} v{revision.version}</strong><span>{revision.status}</span></div><small>{revision.diff || "No diff summary"} · {revision.author}</small>{revision.status === "active" ? <button disabled={busyId === revision.id} onClick={() => void revisionAction(revision.id, "rollback")} type="button">Roll back</button> : null}</div>)}{!revisions.length ? <p>No learned changes yet. Evaluated improvements will appear here.</p> : null}</div></div>
      </div>
    </section>
  );
}
