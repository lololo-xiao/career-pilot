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

export interface CandidateProfile {
  id?: string;
  display_name: string;
  headline: string;
  email: string;
  phone: string;
  claims: ProfileClaim[];
  target_roles: string[];
  preferred_locations: string[];
  languages: string[];
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
  };
  score?: number;
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
