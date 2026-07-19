import type { AuthSessionResponse, AuthUser, ProviderMethod } from "../types";


export type DiscoveryProvider = "greenhouse" | "lever";

export interface DiscoveredJob {
  title: string;
  company: string;
  locations: string[];
  description: string;
  requirements: string[];
  preferred: string[];
  seniority: string;
  employment_type: string;
  workplace_type: "onsite" | "hybrid" | "remote" | "unknown";
  company_size: string;
  posted_date: string | null;
  deadline: string | null;
  source_url: string;
  source_type: string;
}

export interface PublicDiscoveryResponse {
  activity: {
    type: "public_network_read";
    provider: DiscoveryProvider;
    company_identifier: string;
  };
  discovered: number;
  returned: number;
  jobs: DiscoveredJob[];
  stored: 0;
}

export interface PreviewBinding {
  accountId: string;
  provider: DiscoveryProvider;
  companyIdentifier: string;
  generation: number;
}

export interface PreviewRequestTicket {
  generation: number;
  signal: AbortSignal;
  isCurrent: () => boolean;
}

export interface SaveResult {
  created: boolean;
}

export interface SaveOutcome {
  key: string;
  state: "saved" | "already" | "failed";
  message: string;
}

export interface SequentialSaveResult {
  outcomes: SaveOutcome[];
  accountChanged: boolean;
  currentAccountId: string | null;
}

const MAX_DISCOVERED_COUNT = 1_000_000;
const JOB_FIELDS = [
  "title",
  "company",
  "locations",
  "description",
  "requirements",
  "preferred",
  "seniority",
  "employment_type",
  "workplace_type",
  "company_size",
  "posted_date",
  "deadline",
  "source_url",
  "source_type",
] as const;
const COMPANY_SIZES = new Set([
  "1-10",
  "11-50",
  "51-200",
  "201-500",
  "501-1000",
  "1001-5000",
  "5001-10000",
  "10001+",
  "unknown",
]);
const WORKPLACE_TYPES = new Set(["onsite", "hybrid", "remote", "unknown"]);
const PROVIDER_METHODS = new Set<ProviderMethod>(["api_key", "codex"]);


function invalidResponse(kind: string): Error {
  return new Error(`${kind} returned an invalid response.`);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length
    && actual.every((key, index) => key === expected[index]);
}

function boundedString(value: unknown, min: number, max: number): value is string {
  return typeof value === "string" && value.length >= min && value.length <= max;
}

function boundedStringArray(
  value: unknown,
  maxItems: number,
  maxCharacters: number,
): value is string[] {
  return Array.isArray(value)
    && value.length <= maxItems
    && value.every((item) => boundedString(item, 1, maxCharacters));
}

function isDateOrNull(value: unknown): value is string | null {
  if (value === null) return true;
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const date = new Date(`${value}T00:00:00.000Z`);
  return !Number.isNaN(date.valueOf()) && date.toISOString().slice(0, 10) === value;
}

function isSafePublicUrl(value: unknown): value is string {
  if (!boundedString(value, 1, 2_000)) return false;
  try {
    const url = new URL(value);
    return (url.protocol === "https:" || url.protocol === "http:")
      && !url.username
      && !url.password
      && Boolean(url.hostname);
  } catch {
    return false;
  }
}

function parseJob(value: unknown, provider: DiscoveryProvider): DiscoveredJob {
  if (!isRecord(value) || !hasExactKeys(value, JOB_FIELDS)) {
    throw invalidResponse("Public job preview");
  }
  if (
    !boundedString(value.title, 1, 500)
    || !boundedString(value.company, 1, 300)
    || !boundedStringArray(value.locations, 100, 500)
    || !boundedString(value.description, 0, 50_000)
    || !boundedStringArray(value.requirements, 200, 1_000)
    || !boundedStringArray(value.preferred, 200, 1_000)
    || !boundedString(value.seniority, 1, 100)
    || !boundedString(value.employment_type, 1, 100)
    || typeof value.workplace_type !== "string"
    || !WORKPLACE_TYPES.has(value.workplace_type)
    || typeof value.company_size !== "string"
    || !COMPANY_SIZES.has(value.company_size)
    || !isDateOrNull(value.posted_date)
    || !isDateOrNull(value.deadline)
    || !isSafePublicUrl(value.source_url)
    || value.source_type !== provider
  ) {
    throw invalidResponse("Public job preview");
  }
  return {
    title: value.title,
    company: value.company,
    locations: value.locations,
    description: value.description,
    requirements: value.requirements,
    preferred: value.preferred,
    seniority: value.seniority,
    employment_type: value.employment_type,
    workplace_type: value.workplace_type as DiscoveredJob["workplace_type"],
    company_size: value.company_size,
    posted_date: value.posted_date,
    deadline: value.deadline,
    source_url: value.source_url,
    source_type: provider,
  };
}

function nullableBoundedString(value: unknown, max: number): value is string | null {
  return value === null || boundedString(value, 0, max);
}

function parseAuthUser(value: unknown): AuthUser {
  const keys = [
    "id",
    "display_name",
    "active_provider",
    "plan_type",
    "provider_email",
    "provider_label",
  ];
  if (
    !isRecord(value)
    || !hasExactKeys(value, keys)
    || !boundedString(value.id, 1, 200)
    || !boundedString(value.display_name, 0, 200)
    || !(value.active_provider === null
      || (typeof value.active_provider === "string"
        && PROVIDER_METHODS.has(value.active_provider as ProviderMethod)))
    || !nullableBoundedString(value.plan_type, 100)
    || !nullableBoundedString(value.provider_email, 320)
    || !nullableBoundedString(value.provider_label, 100)
  ) {
    throw invalidResponse("Local session");
  }
  return {
    id: value.id,
    display_name: value.display_name,
    active_provider: value.active_provider as ProviderMethod | null,
    plan_type: value.plan_type,
    provider_email: value.provider_email,
    provider_label: value.provider_label,
  };
}

export function parseLocalSessionResponse(value: unknown): AuthSessionResponse {
  if (!isRecord(value) || !hasExactKeys(value, ["authenticated", "user"])) {
    throw invalidResponse("Local session");
  }
  if (value.authenticated === false && value.user === null) {
    return { authenticated: false, user: null };
  }
  if (value.authenticated !== true) throw invalidResponse("Local session");
  return { authenticated: true, user: parseAuthUser(value.user) };
}

export function parsePublicDiscoveryResponse(
  value: unknown,
  expectedProvider: DiscoveryProvider,
  expectedIdentifier: string,
  requestedLimit: number,
): PublicDiscoveryResponse {
  if (
    !isRecord(value)
    || !hasExactKeys(value, ["activity", "discovered", "returned", "jobs", "stored"])
    || !isRecord(value.activity)
    || !hasExactKeys(value.activity, ["type", "provider", "company_identifier"])
    || value.activity.type !== "public_network_read"
    || value.activity.provider !== expectedProvider
    || value.activity.company_identifier !== normalizeCompanyIdentifier(expectedIdentifier)
    || !Number.isInteger(value.discovered)
    || (value.discovered as number) < 0
    || (value.discovered as number) > MAX_DISCOVERED_COUNT
    || !Number.isInteger(value.returned)
    || (value.returned as number) < 0
    || (value.returned as number) > 25
    || (value.returned as number) > requestedLimit
    || (value.returned as number) > (value.discovered as number)
    || value.stored !== 0
    || !Array.isArray(value.jobs)
    || value.jobs.length !== value.returned
    || value.jobs.length > 25
  ) {
    throw invalidResponse("Public job preview");
  }
  const jobs = value.jobs.map((job) => parseJob(job, expectedProvider));
  if (new Set(jobs.map((job) => job.source_url)).size !== jobs.length) {
    throw invalidResponse("Public job preview");
  }
  return {
    activity: {
      type: "public_network_read",
      provider: expectedProvider,
      company_identifier: normalizeCompanyIdentifier(expectedIdentifier),
    },
    discovered: value.discovered as number,
    returned: value.returned as number,
    jobs,
    stored: 0,
  };
}

export function parseJobWriteResult(value: unknown): SaveResult {
  if (
    !isRecord(value)
    || typeof value.created !== "boolean"
    || !boundedString(value.id, 1, 200)
  ) {
    throw invalidResponse("Job save");
  }
  return { created: value.created };
}

export class PreviewRequestCoordinator {
  private active: { generation: number; controller: AbortController } | null = null;
  private nextGeneration = 0;

  start(): PreviewRequestTicket {
    this.invalidate();
    const controller = new AbortController();
    const generation = this.nextGeneration;
    this.active = { generation, controller };
    return {
      generation,
      signal: controller.signal,
      isCurrent: () => this.active?.generation === generation,
    };
  }

  invalidate(): number {
    this.active?.controller.abort();
    this.active = null;
    this.nextGeneration += 1;
    return this.nextGeneration;
  }

  finish(generation: number): void {
    if (this.active?.generation === generation) this.active = null;
  }

  currentGeneration(): number {
    return this.active?.generation ?? this.nextGeneration;
  }
}

export function normalizeCompanyIdentifier(value: string): string {
  return value.trim();
}

export function jobKey(job: DiscoveredJob): string {
  return job.source_url;
}

export function previewBindingMatches(
  binding: PreviewBinding,
  accountId: string,
  provider: DiscoveryProvider,
  companyIdentifier: string,
  generation: number,
): boolean {
  return binding.accountId === accountId
    && binding.provider === provider
    && binding.companyIdentifier === normalizeCompanyIdentifier(companyIdentifier)
    && binding.generation === generation;
}

export function snapshotSelectedJobs(
  jobs: DiscoveredJob[],
  selectedKeys: ReadonlySet<string>,
): DiscoveredJob[] {
  return jobs
    .filter((job) => selectedKeys.has(jobKey(job)))
    .map((job) => ({
      ...job,
      locations: [...job.locations],
      requirements: [...job.requirements],
      preferred: [...job.preferred],
    }));
}

export function jobWritePayload(job: DiscoveredJob): object {
  return { spec: job, canonical_url: job.source_url };
}

export async function saveJobSnapshotSequentially(
  jobs: readonly DiscoveredJob[],
  expectedAccountId: string,
  readAccountId: () => Promise<string | null>,
  saveJob: (job: DiscoveredJob) => Promise<SaveResult>,
  onOutcome?: (outcome: SaveOutcome) => void,
): Promise<SequentialSaveResult> {
  const outcomes: SaveOutcome[] = [];
  for (const job of jobs) {
    const currentAccountId = await readAccountId();
    if (currentAccountId !== expectedAccountId) {
      return { outcomes, accountChanged: true, currentAccountId };
    }

    const key = jobKey(job);
    let outcome: SaveOutcome;
    try {
      const result = await saveJob(job);
      outcome = {
        key,
        state: result.created ? "saved" : "already",
        message: result.created ? "Saved" : "Already in queue",
      };
    } catch (error) {
      outcome = {
        key,
        state: "failed",
        message: error instanceof Error ? error.message : "Save not confirmed — retry safely",
      };
    }
    outcomes.push(outcome);
    onOutcome?.(outcome);
  }
  return { outcomes, accountChanged: false, currentAccountId: expectedAccountId };
}
