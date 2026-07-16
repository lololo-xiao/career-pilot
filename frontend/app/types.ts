export type RequirementImportance = "required" | "preferred" | "unspecified";

export interface EvidenceCitation {
  source_id: string;
  quote: string;
}

export interface MatchedSkill {
  skill: string;
  explanation: string;
  evidence: EvidenceCitation[];
}

export type AdjacentSkill = MatchedSkill;

export interface MissingSkill {
  skill: string;
  importance: RequirementImportance;
  explanation: string;
}

export interface JobRequirement {
  requirement: string;
  importance: RequirementImportance;
  evidence_quote: string;
}

export interface PreparationAction {
  priority: number;
  action: string;
  rationale: string;
  addresses: string[];
}

export interface UnsupportedClaimWarning {
  claim: string;
  reason: string;
}

export interface MatchResponse {
  score: number;
  summary: string;
  matched_skills: MatchedSkill[];
  adjacent_skills: AdjacentSkill[];
  missing_skills: MissingSkill[];
  important_requirements: JobRequirement[];
  preparation_actions: PreparationAction[];
  unsupported_claim_warnings: UnsupportedClaimWarning[];
}

export interface CompanionTurn {
  role: "user" | "assistant";
  content: string;
}

export interface CompanionChatResponse {
  message: string;
  suggested_prompts: string[];
}

export interface ParsedCVResponse {
  filename: string;
  file_type: "pdf" | "docx" | "text";
  text: string;
  character_count: number;
  page_count: number | null;
}

export type ProviderMethod = "api_key" | "codex";

export interface AuthUser {
  id: string;
  display_name: string;
  active_provider: ProviderMethod | null;
  plan_type: string | null;
  provider_email: string | null;
  provider_label: string | null;
}

export interface AuthSessionResponse {
  authenticated: boolean;
  user: AuthUser | null;
}

export interface ProviderConnection {
  provider: ProviderMethod;
  connected: boolean;
  active: boolean;
  provider_label: string;
  provider_email: string | null;
  plan_type: string | null;
}

export interface ProviderSettingsResponse {
  active_provider: ProviderMethod | null;
  connections: ProviderConnection[];
}

export type CapabilityState =
  | "enabled"
  | "setup_required"
  | "limited"
  | "disabled";

export interface CapabilityGroup {
  id: string;
  name: string;
  description: string;
  state: CapabilityState;
  state_label: string;
  tools: string[];
  note: string | null;
}

export interface MCPServerCapability {
  name: string;
  display_name: string;
  enabled: boolean;
  transport: "http" | "stdio";
  tools: string[];
}

export interface CapabilitySettingsResponse {
  enabled_toolsets: string[];
  disabled_toolsets: string[];
  groups: CapabilityGroup[];
  mcp_servers: MCPServerCapability[];
  web_search: {
    provider: "brave";
    provider_label: string;
    configured: boolean;
    setup_url: string;
    key_storage: "encrypted_local";
  };
}

export type ReasoningEffort = "none" | "minimal" | "low" | "medium" | "high" | "xhigh";

export interface AgentReasoningEffortOption {
  reasoning_effort: ReasoningEffort;
  description: string;
}

export interface AgentModelOption {
  model: string;
  display_name: string;
  description: string;
  is_default: boolean;
  default_reasoning_effort: ReasoningEffort;
  supported_reasoning_efforts: AgentReasoningEffortOption[];
  context_window: number | null;
}

export interface AgentRateLimitWindow {
  used_percent: number;
  resets_at: number | null;
  window_duration_minutes: number | null;
}

export interface AgentRateLimits {
  plan_type: string | null;
  limit_name: string | null;
  reached_type: string | null;
  primary: AgentRateLimitWindow | null;
  secondary: AgentRateLimitWindow | null;
  individual: {
    limit: string;
    used: string;
    remaining_percent: number;
    resets_at: number;
  } | null;
  credits: {
    has_credits: boolean;
    unlimited: boolean;
    balance: string | null;
  } | null;
}

export interface AgentSettingsResponse {
  provider: "openai-api" | "openai-codex";
  model: string;
  reasoning_effort: ReasoningEffort;
  token_limit: number;
  models: AgentModelOption[];
  rate_limits: AgentRateLimits | null;
  account_usage: {
    lifetime_tokens: number | null;
    peak_daily_tokens: number | null;
    current_streak_days: number | null;
  } | null;
  warnings: string[];
}

export interface CodexLoginStartResponse {
  attempt_id: string;
  verification_url: string;
  user_code: string;
  expires_at: number;
}

export interface CodexLoginStatusResponse {
  status: "pending" | "completed" | "failed" | "expired";
  user: AuthUser | null;
  error: string | null;
}
