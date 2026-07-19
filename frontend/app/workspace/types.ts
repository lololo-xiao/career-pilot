export type ClaimStatus = "verified" | "adjacent" | "learning" | "prohibited";

export interface EvidenceReference {
  source_id: string;
  source_name: string;
  page?: number;
  excerpt: string;
  content_hash: string;
}

export interface ProfileClaim {
  key: string;
  value: string;
  status: ClaimStatus;
  evidence: EvidenceReference[];
  confidence: number;
  user_verified_at?: string;
}

export interface ProjectAnalysis {
  source: "local" | "github";
  repository_name: string;
  analyzed_at: string;
  file_count: number;
  primary_languages: string[];
  technologies: string[];
  notable_files: string[];
  improvement_suggestions: string[];
  interview_questions: string[];
  summary: string;
}

export interface ProfileProject {
  id: string;
  name: string;
  description: string;
  repository_url?: string;
  local_path: string;
  technologies: string[];
  highlights: string[];
  analysis?: ProjectAnalysis;
}

export interface CandidateProfile {
  id?: string;
  display_name: string;
  seniority: string;
  email: string;
  phone: string;
  claims: ProfileClaim[];
  target_roles: string[];
  preferred_locations: string[];
  languages: string[];
  projects: ProfileProject[];
  work_authorization: ProfileClaim[];
  source_documents: string[];
  updated_at?: string;
}

export interface Job {
  id: string;
  company: string;
  title: string;
  canonical_url: string;
  source_type: string;
  spec: {
    title: string;
    company: string;
    locations: string[];
    description: string;
    employment_type?: string;
    workplace_type?: "onsite" | "hybrid" | "remote" | "unknown";
    company_size?:
      | "1-10"
      | "11-50"
      | "51-200"
      | "201-500"
      | "501-1000"
      | "1001-5000"
      | "5001-10000"
      | "10001+"
      | "unknown";
    posted_date?: string;
    deadline?: string;
    source_url?: string;
  };
  score?: number | null;
  tier?: string;
  score_explanation: string[];
  created_at?: string;
}

export interface Artifact {
  id: string;
  kind: string;
  version: number;
  sha256: string;
  approved: boolean;
}

export interface Application {
  id: string;
  job_id: string;
  status: string;
  next_action: string;
  next_action_at?: string;
  submitted_at?: string;
  artifacts: Artifact[];
  status_events: Array<{
    from: string;
    to: string;
    note: string;
    created_at: string;
  }>;
  created_at?: string;
  updated_at?: string;
}

export type FormFieldKey =
  | "full_name"
  | "email"
  | "phone"
  | "location"
  | "work_authorization"
  | "resume"
  | "cover_letter"
  | "unsupported";

export type FormFieldState = "mapped" | "unknown" | "unsupported" | "ambiguous";

export interface FormPreviewFieldSpec {
  field_id: string;
  label: string;
  field_key: FormFieldKey;
  required: boolean;
  options: string[];
}

export interface FormPreviewField extends FormPreviewFieldSpec {
  state: FormFieldState;
  value: string | null;
  source: Record<string, unknown> | null;
  reason: string;
}

export interface FormPreview {
  id: string;
  schema_version: "local-form-fill-preview-v1";
  application_id: string;
  form_reference: string;
  request_digest: string;
  evidence_digest: string;
  mode: "preview_only";
  all_required_mapped: boolean;
  unresolved_required_count: number;
  fields: FormPreviewField[];
  source: Record<string, unknown>;
  safety: {
    browser_used: false;
    navigation_performed: false;
    controls_clicked: false;
    files_uploaded: false;
    form_filled: false;
    submitted: false;
    external_mutation_performed: false;
    later_external_phase_implemented: false;
    later_explicit_confirmation_required: true;
  };
  created?: boolean;
}

export interface ModelRoute {
  name: string;
  provider: "openai-api" | "openai-codex";
  model: string;
  reasoning_effort: "none" | "minimal" | "low" | "medium" | "high" | "xhigh";
  token_limit: number;
  cost_budget_usd: number;
  fallback_policy: "none" | "same-provider" | "explicit";
  fallback_route?: string;
  scheduled: boolean;
}

export interface Revision {
  id: string;
  kind: "memory" | "skill" | "rubric";
  name: string;
  version: number;
  diff: string;
  author: string;
  source_session: string;
  status: "draft" | "active" | "quarantined" | "rolled_back";
  evaluation: Record<string, unknown>;
  created_at: string;
}

export interface Approval {
  id: string;
  action_type: string;
  preview: Record<string, unknown>;
  expires_at: string;
  decision: "pending" | "approved" | "denied" | "expired" | "consumed";
  created_at: string;
}

export interface CompanionSettings {
  daily_cost_usd: number;
  daily_api_budget_usd: number;
  adjacent_claims_allowed: boolean;
  claim_posture: string;
}
