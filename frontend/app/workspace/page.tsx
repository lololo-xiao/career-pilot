"use client";

import {
  FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import type { AuthSessionResponse, AuthUser } from "../types";
import { API_BASE_URL, apiRequest, openArtifact } from "./api";
import type {
  Application,
  Approval,
  CandidateProfile,
  CompanionSettings,
  Job,
  ModelRoute,
  ProfileClaim,
  Revision,
} from "./types";

type WorkspaceTab =
  | "overview"
  | "profile"
  | "jobs"
  | "applications"
  | "controls";

const EMPTY_PROFILE: CandidateProfile = {
  display_name: "",
  headline: "",
  email: "",
  phone: "",
  claims: [],
  target_roles: [],
  preferred_locations: [],
  languages: [],
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
    languages: Array.from(new Set([...(profile.languages ?? []), ...importedLanguages])),
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
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [settings, setSettings] = useState<CompanionSettings | null>(null);

  const refresh = useCallback(async (quiet = false) => {
    if (!quiet) setRefreshing(true);
    try {
      const [jobRows, applicationRows, profileRow, routeRows, revisionRows, approvalRows, settingsRow] =
        await Promise.all([
          apiRequest<Job[]>("/api/v1/jobs"),
          apiRequest<Application[]>("/api/v1/applications"),
          apiRequest<CandidateProfile | null>("/api/v1/onboarding/profile"),
          apiRequest<ModelRoute[]>("/api/v1/model-routes"),
          apiRequest<Revision[]>("/api/v1/revisions"),
          apiRequest<Approval[]>("/api/v1/approvals"),
          apiRequest<CompanionSettings>("/api/v1/settings"),
        ]);
      setJobs(jobRows);
      setApplications(applicationRows);
      setProfile(normalizeProfile(profileRow ?? EMPTY_PROFILE));
      setRoutes(routeRows);
      setRevisions(revisionRows);
      setApprovals(approvalRows);
      setSettings(settingsRow);
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
          <div><strong>Stored on this device</strong><span>Account-isolated workspace</span></div>
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
              applications={applications}
              approvals={approvals}
              jobs={jobs}
              profile={profile}
              settings={settings}
              setTab={setActiveTab}
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
              jobById={jobById}
              refresh={refresh}
              setError={setError}
            />
          ) : null}
          {activeTab === "controls" ? (
            <ControlsPanel
              key={routes.map((route) => `${route.name}:${route.provider}:${route.model}:${route.reasoning_effort}:${route.cost_budget_usd}`).join("|")}
              approvals={approvals}
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
  applications,
  approvals,
  jobs,
  profile,
  settings,
  setTab,
}: {
  applications: Application[];
  approvals: Approval[];
  jobs: Job[];
  profile: CandidateProfile;
  settings: CompanionSettings | null;
  setTab: (tab: WorkspaceTab) => void;
}) {
  const activeApplications = applications.filter(
    (item) => !["offer", "rejected", "withdrawn"].includes(item.status),
  );
  const pendingApprovals = approvals.filter((item) => item.decision === "pending");
  const verifiedClaims = [...profile.claims, ...profile.work_authorization].filter(
    (claim) => claim.status === "verified",
  ).length;
  const ranked = jobs.filter((job) => typeof job.score === "number");
  const bestJob = [...ranked].sort((a, b) => (b.score ?? 0) - (a.score ?? 0))[0];

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
      <div className="workspace-grid-two">
        <article className="workspace-card workspace-card-accent">
          <span className="workspace-kicker">NEXT BEST STEP</span>
          <h2>{bestJob ? `Review ${bestJob.title} at ${bestJob.company}` : "Add your first opportunity"}</h2>
          <p>
            {bestJob
              ? `It currently leads your queue with a score of ${bestJob.score}. Open the evidence before deciding.`
              : "Paste a job description. Pilot will deduplicate it and apply deterministic scoring."}
          </p>
          <button onClick={() => setTab("jobs")} type="button">
            {bestJob ? "Open ranked jobs" : "Add a job"}
          </button>
        </article>
        <article className="workspace-card">
          <span className="workspace-kicker">PROFILE FACTS</span>
          <h2>{profile.claims.length ? `${verifiedClaims} ${verifiedClaims === 1 ? "fact is" : "facts are"} ready to use.` : "Import your career facts."}</h2>
          <p>
            Correct the imported details, compare them with the source, and confirm
            only the facts you want Pilot to use.
          </p>
          <button onClick={() => setTab("profile")} type="button">Review career facts</button>
        </article>
      </div>
      {pendingApprovals.length ? (
        <div className="workspace-notice">
          <strong>{pendingApprovals.length} action{pendingApprovals.length === 1 ? "" : "s"} waiting for approval</strong>
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

  function updateList(field: "target_roles" | "preferred_locations" | "languages", value: string) {
    const separator = field === "preferred_locations" ? ";" : ",";
    setProfile({
      ...profile,
      [field]: value
        .split(separator)
        .map((item) => item.trim())
        .filter(Boolean),
    });
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
          <p>PDF or DOCX, up to 20 MB. The original and extracted text stay on this device.</p>
          <input accept=".pdf,.docx" name="file" required type="file" />
          <button disabled={busy} type="submit">{busy ? "Reading…" : "Import details"}</button>
        </form>
        <div className="workspace-card workspace-form-grid">
          <label>Your name<input value={profile.display_name} onChange={(event) => setProfile({ ...profile, display_name: event.target.value })} /></label>
          <label>Headline<input value={profile.headline} onChange={(event) => setProfile({ ...profile, headline: event.target.value })} /></label>
          <label>Email<input inputMode="email" value={profile.email} onChange={(event) => setProfile({ ...profile, email: event.target.value })} /></label>
          <label>Phone<input inputMode="tel" value={profile.phone} onChange={(event) => setProfile({ ...profile, phone: event.target.value })} /></label>
          <label>Target roles<input value={profile.target_roles.join(", ")} onChange={(event) => updateList("target_roles", event.target.value)} /></label>
          <label>Preferred locations<input placeholder="Berlin, Germany; Munich, Germany" value={profile.preferred_locations.join("; ")} onChange={(event) => updateList("preferred_locations", event.target.value)} /></label>
          <label className="workspace-wide">Languages<input value={profile.languages.join(", ")} onChange={(event) => updateList("languages", event.target.value)} /></label>
        </div>
      </div>
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

  const sortedJobs = [...jobs].sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
  return (
    <section>
      <div className="workspace-section-heading"><div><span className="workspace-eyebrow">FOCUSED SEARCH</span><h1>Your job queue.</h1><p>Deterministic scoring first, with a visible reason for every recommendation.</p></div><button onClick={() => setShowForm((value) => !value)} type="button">{showForm ? "Close form" : "Add a job"}</button></div>
      {showForm ? (
        <form className="workspace-card workspace-job-form" onSubmit={addJob}>
          <label>Company<input name="company" required /></label>
          <label>Role<input name="title" required /></label>
          <label>Locations<input name="location" placeholder="Berlin, Germany; Munich, Germany" /></label>
          <label>Job URL<input name="url" type="url" /></label>
          <label className="workspace-wide">Job description<textarea name="description" required rows={10} /></label>
          <button disabled={busyId === "new"} type="submit">{busyId === "new" ? "Saving…" : "Save opportunity"}</button>
        </form>
      ) : null}
      <div className="workspace-stack">
        {sortedJobs.map((job) => (
          <article className="workspace-card workspace-job-card" key={job.id}>
            <div className={`workspace-score workspace-tier-${job.tier ?? "none"}`}><strong>{job.score ?? "—"}</strong><span>{job.tier ? `Tier ${job.tier}` : "Not scored"}</span></div>
            <div className="workspace-job-copy"><span>{job.company}</span><h2>{job.title}</h2><p>{job.spec.locations.join(" · ") || "Location not listed"}</p>{job.score_explanation.length ? <details><summary>Why this score?</summary><ul>{job.score_explanation.map((reason) => <li key={reason}>{reason}</li>)}</ul></details> : null}</div>
            <div className="workspace-actions"><button disabled={busyId === job.id} onClick={() => void act(`/api/v1/jobs/${job.id}/score`, job.id)} type="button">Score</button><button disabled={busyId === job.id || trackedJobs.has(job.id)} onClick={() => void act(`/api/v1/applications?job_id=${job.id}`, job.id)} type="button">{trackedJobs.has(job.id) ? "Tracked" : "Track application"}</button></div>
          </article>
        ))}
        {!jobs.length ? <div className="workspace-empty workspace-card"><strong>Your queue is empty.</strong><span>Add a job description to start ranking opportunities.</span></div> : null}
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
  jobById,
  refresh,
  setError,
}: {
  applications: Application[];
  jobById: Map<string, Job>;
  refresh: (quiet?: boolean) => Promise<void>;
  setError: (message: string | null) => void;
}) {
  const [busyId, setBusyId] = useState<string | null>(null);

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

  return (
    <section>
      <div className="workspace-section-heading"><div><span className="workspace-eyebrow">CONTROLLED PROGRESS</span><h1>Applications.</h1><p>Pilot can prepare and fill. You review, approve, and submit.</p></div></div>
      <div className="workspace-stack">
        {applications.map((application) => {
          const job = jobById.get(application.job_id);
          const next = NEXT_STATUS[application.status];
          return (
            <article className="workspace-card workspace-application" key={application.id}>
              <div className="workspace-card-heading">
                <div><span className="workspace-status">{application.status.replaceAll("_", " ")}</span><h2>{job?.title ?? "Application"}</h2><p>{job?.company ?? "Unknown company"} · {application.next_action}</p></div>
                <div className="workspace-actions">
                  {next ? <button disabled={busyId === application.id} onClick={() => void post(`/api/v1/applications/${application.id}/status`, application.id, { status: next.target, note: "Confirmed in local workspace" })} type="button">{next.label}</button> : null}
                  {application.status === "approved" ? <button disabled={busyId === application.id} onClick={() => void post(`/api/v1/applications/${application.id}/artifacts/generate`, application.id)} type="button">Generate application pack</button> : null}
                  {["ready", "form_filled"].includes(application.status) ? <button className="workspace-danger-safe" disabled={busyId === application.id} onClick={() => void post(`/api/v1/applications/${application.id}/status`, application.id, { status: "submitted", note: "User confirmed manual submission", confirmed_by_user: true })} type="button">I submitted it manually</button> : null}
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
            </article>
          );
        })}
        {!applications.length ? <div className="workspace-empty workspace-card"><strong>No applications tracked yet.</strong><span>Add a job, score it, then choose “Track application.”</span></div> : null}
      </div>
    </section>
  );
}

function ControlsPanel({
  approvals,
  refresh,
  revisions,
  routes,
  setError,
  settings,
}: {
  approvals: Approval[];
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
        <div className="workspace-card"><div className="workspace-card-heading"><div><h2>Approval history</h2><p>Approvals are payload-bound, expiring, and single-use.</p></div></div><div className="workspace-history">{approvals.slice().reverse().map((approval) => <div key={approval.id}><div><strong>{approval.action_type.replaceAll(".", " ")}</strong><span>{approval.decision}</span></div><small>Expires {new Date(approval.expires_at).toLocaleString()}</small></div>)}{!approvals.length ? <p>No external actions have requested approval.</p> : null}</div></div>
        <div className="workspace-card"><div className="workspace-card-heading"><div><h2>Evolution history</h2><p>Only memory, user-owned skills, and rubrics can evolve.</p></div></div><div className="workspace-history">{revisions.slice().reverse().map((revision) => <div key={revision.id}><div><strong>{revision.kind}: {revision.name} v{revision.version}</strong><span>{revision.status}</span></div><small>{revision.diff || "No diff summary"} · {revision.author}</small>{revision.status === "active" ? <button disabled={busyId === revision.id} onClick={() => void revisionAction(revision.id, "rollback")} type="button">Roll back</button> : null}</div>)}{!revisions.length ? <p>No learned changes yet. Evaluated improvements will appear here.</p> : null}</div></div>
      </div>
    </section>
  );
}
