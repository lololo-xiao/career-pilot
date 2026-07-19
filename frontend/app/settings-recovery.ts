import type {
  AgentAsset,
  AgentAssetSettingsResponse,
  AgentIdentity,
  AuthSessionResponse,
  AuthUser,
  CapabilityGroup,
  CapabilitySettingsResponse,
  MCPServerCapability,
  MCPServerSettings,
  MCPSettingsResponse,
  ProviderConnection,
  ProviderSettingsResponse,
} from "./types";


export const SETTINGS_STEPS = [
  {
    id: "ai-connection",
    label: "AI connection",
    description: "Choose how Pilot runs",
    targetId: "settings-step-ai",
  },
  {
    id: "identity-soul",
    label: "Identity / Soul",
    description: "Shape your companion",
    targetId: "settings-step-identity",
  },
  {
    id: "memories-skills",
    label: "Memories & Skills",
    description: "Review local context",
    targetId: "settings-step-resources",
  },
  {
    id: "mcp",
    label: "MCP (optional)",
    description: "Connect selected tools",
    targetId: "settings-step-mcp",
  },
  {
    id: "capability-review",
    label: "Capability review",
    description: "Confirm Pilot’s reach",
    targetId: "settings-step-capabilities",
  },
] as const;

export type SettingsStepId = (typeof SETTINGS_STEPS)[number]["id"];
export const SETTINGS_READ_SECTIONS = [
  "session",
  "providers",
  "identity",
  "resources",
  "mcp",
  "capabilities",
] as const;

export type SettingsReadSection = (typeof SETTINGS_READ_SECTIONS)[number];

const SETTINGS_READ_PATHS: Record<SettingsReadSection, string> = {
  session: "/local/session",
  providers: "/settings/providers",
  identity: "/settings/agent-identity",
  resources: "/settings/agent-resources",
  mcp: "/settings/mcp",
  capabilities: "/settings/capabilities",
};

export function settingsReadEndpoint(
  apiBaseUrl: string,
  section: SettingsReadSection,
): string {
  return `${apiBaseUrl.replace(/\/$/, "")}${SETTINGS_READ_PATHS[section]}`;
}

export interface SettingsNavigatorState {
  currentStep: SettingsStepId;
}

export type SettingsNavigatorAction =
  | { type: "select"; step: SettingsStepId }
  | { type: "move"; direction: "next" | "previous" | "first" | "last" };

export function settingsNavigatorReducer(
  state: SettingsNavigatorState,
  action: SettingsNavigatorAction,
): SettingsNavigatorState {
  if (action.type === "select") {
    return action.step === state.currentStep ? state : { currentStep: action.step };
  }

  const currentIndex = SETTINGS_STEPS.findIndex((step) => step.id === state.currentStep);
  const lastIndex = SETTINGS_STEPS.length - 1;
  let nextIndex = currentIndex < 0 ? 0 : currentIndex;
  if (action.direction === "first") nextIndex = 0;
  if (action.direction === "last") nextIndex = lastIndex;
  if (action.direction === "next") nextIndex = Math.min(nextIndex + 1, lastIndex);
  if (action.direction === "previous") nextIndex = Math.max(nextIndex - 1, 0);
  const currentStep = SETTINGS_STEPS[nextIndex]?.id ?? SETTINGS_STEPS[0].id;
  return currentStep === state.currentStep ? state : { currentStep };
}

export function nextSettingsStep(step: SettingsStepId): SettingsStepId | null {
  const index = SETTINGS_STEPS.findIndex((candidate) => candidate.id === step);
  return index >= 0 && index < SETTINGS_STEPS.length - 1
    ? SETTINGS_STEPS[index + 1].id
    : null;
}

export type SettingsNavigationMode = "activate" | "roving";

export interface SettingsNavigationIntent {
  focusContent: boolean;
  scrollContent: boolean;
  updateHash: boolean;
}

export function getSettingsNavigationIntent(
  mode: SettingsNavigationMode,
): SettingsNavigationIntent {
  const activateContent = mode === "activate";
  return {
    focusContent: activateContent,
    scrollContent: activateContent,
    updateHash: true,
  };
}

export type RecoverableReadStatus = "idle" | "loading" | "ready" | "error";

export interface RecoverableReadState<T> {
  data: T | null;
  error: string | null;
  generation: number;
  status: RecoverableReadStatus;
}

export type RecoverableReadAction<T> =
  | { type: "start"; generation: number }
  | { type: "success"; generation: number; data: T }
  | { type: "failure"; generation: number; error: string };

export function initialRecoverableReadState<T>(data: T | null = null): RecoverableReadState<T> {
  return {
    data,
    error: null,
    generation: 0,
    status: data === null ? "idle" : "ready",
  };
}

export function recoverableReadReducer<T>(
  state: RecoverableReadState<T>,
  action: RecoverableReadAction<T>,
): RecoverableReadState<T> {
  if (action.type === "start") {
    // Starts are dispatched synchronously after the external generation ref advances.
    // Numeric ordering is intentionally not used because the token wraps MAX_SAFE_INTEGER → 1.
    return {
      ...state,
      error: null,
      generation: action.generation,
      status: "loading",
    };
  }
  if (action.generation !== state.generation) return state;
  if (action.type === "success") {
    return {
      data: action.data,
      error: null,
      generation: state.generation,
      status: "ready",
    };
  }
  return {
    ...state,
    error: action.error,
    status: "error",
  };
}

export function nextReadGeneration(current: number): number {
  return current >= Number.MAX_SAFE_INTEGER ? 1 : current + 1;
}

export function isCurrentReadGeneration(candidate: number, current: number): boolean {
  return candidate === current;
}

export interface SettingsReadTicket {
  readonly generation: number;
  readonly section: SettingsReadSection;
}

export interface SettingsReadController {
  readonly section: SettingsReadSection;
  begin: () => SettingsReadTicket;
  commit: (ticket: SettingsReadTicket, effect: () => void) => boolean;
  invalidate: (ticket: SettingsReadTicket) => void;
}

export function createSettingsReadController(
  section: SettingsReadSection,
  initialGeneration = 0,
): SettingsReadController {
  let generation = initialGeneration;
  return {
    section,
    begin() {
      generation = nextReadGeneration(generation);
      return { generation, section };
    },
    commit(ticket, effect) {
      if (
        ticket.section !== section
        || !isCurrentReadGeneration(ticket.generation, generation)
      ) return false;
      effect();
      return true;
    },
    invalidate(ticket) {
      if (
        ticket.section === section
        && isCurrentReadGeneration(ticket.generation, generation)
      ) {
        generation = nextReadGeneration(generation);
      }
    },
  };
}

export interface SettingsDraftHydration {
  hydrate: (effect: () => void) => boolean;
}

export function createSettingsDraftHydration(): SettingsDraftHydration {
  let hydrated = false;
  return {
    hydrate(effect) {
      if (hydrated) return false;
      hydrated = true;
      effect();
      return true;
    },
  };
}

export type SettingsReadErrorKind = "http" | "invalid-json" | "malformed";

export class SettingsReadError extends Error {
  readonly kind: SettingsReadErrorKind;
  readonly status: number | null;

  constructor(message: string, kind: SettingsReadErrorKind, status: number | null = null) {
    super(message);
    this.name = "SettingsReadError";
    this.kind = kind;
    this.status = status;
  }
}

function errorDetail(value: unknown): string | null {
  if (!isRecord(value)) return null;
  if (typeof value.detail === "string" && value.detail.trim()) return value.detail;
  if (!Array.isArray(value.detail)) return null;
  const details = value.detail
    .map((item) => isRecord(item) && typeof item.msg === "string" ? item.msg : null)
    .filter((item): item is string => Boolean(item));
  return details.length > 0 ? details.join(" ") : null;
}

export async function readValidatedSettingsResponse<T>(
  response: Response,
  validate: (value: unknown) => value is T,
  fallback: string,
): Promise<T> {
  const text = await response.text();
  let payload: unknown = null;
  let validJson = false;
  if (text.trim()) {
    try {
      payload = JSON.parse(text) as unknown;
      validJson = true;
    } catch {
      validJson = false;
    }
  }

  if (!response.ok) {
    throw new SettingsReadError(
      (validJson ? errorDetail(payload) : null) ?? fallback,
      "http",
      response.status,
    );
  }
  if (!validJson) {
    throw new SettingsReadError(`${fallback} The response was not valid JSON.`, "invalid-json");
  }
  if (!validate(payload)) {
    throw new SettingsReadError(`${fallback} The response was malformed.`, "malformed");
  }
  return payload;
}

export function settingsReadMessage(error: unknown, fallback: string): string {
  if (error instanceof SettingsReadError) return error.message;
  return fallback;
}

export function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

export type SettingsReadFetcher = (
  input: string,
  init: RequestInit,
) => Promise<Response>;

export interface RecoverableSettingsReadOptions<T> {
  apiBaseUrl: string;
  controller: SettingsReadController;
  fallback: string;
  fetcher?: SettingsReadFetcher;
  onFailure: (message: string, generation: number) => void;
  onStart: (generation: number) => void;
  onSuccess: (payload: T, generation: number) => void;
  signal: AbortSignal;
  validate: (value: unknown) => value is T;
}

export interface RecoverableSettingsRead {
  completion: Promise<void>;
  ticket: SettingsReadTicket;
}

export function startRecoverableSettingsRead<T>(
  options: RecoverableSettingsReadOptions<T>,
): RecoverableSettingsRead {
  const ticket = options.controller.begin();
  options.onStart(ticket.generation);
  const fetcher = options.fetcher ?? ((input, init) => fetch(input, init));
  const completion = (async () => {
    try {
      const response = await fetcher(
        settingsReadEndpoint(options.apiBaseUrl, ticket.section),
        {
          cache: "no-store",
          credentials: "include",
          signal: options.signal,
        },
      );
      const payload = await readValidatedSettingsResponse(
        response,
        options.validate,
        options.fallback,
      );
      options.controller.commit(ticket, () => {
        options.onSuccess(payload, ticket.generation);
      });
    } catch (error) {
      if (isAbortError(error)) return;
      options.controller.commit(ticket, () => {
        options.onFailure(
          settingsReadMessage(error, `${options.fallback} Try again.`),
          ticket.generation,
        );
      });
    }
  })();
  return { completion, ticket };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isProviderMethod(value: unknown): value is "api_key" | "codex" {
  return value === "api_key" || value === "codex";
}

function isAuthUser(value: unknown): value is AuthUser {
  return isRecord(value)
    && typeof value.id === "string"
    && typeof value.display_name === "string"
    && (value.active_provider === null || isProviderMethod(value.active_provider))
    && isNullableString(value.plan_type)
    && isNullableString(value.provider_email)
    && isNullableString(value.provider_label);
}

export function isAuthSessionResponse(value: unknown): value is AuthSessionResponse {
  if (!isRecord(value) || typeof value.authenticated !== "boolean") return false;
  if (value.authenticated === false) return value.user === null;
  return isAuthUser(value.user);
}

export function shouldRedirectForSession(payload: AuthSessionResponse): boolean {
  return payload.authenticated === false && payload.user === null;
}

function isProviderConnection(value: unknown): value is ProviderConnection {
  return isRecord(value)
    && isProviderMethod(value.provider)
    && typeof value.connected === "boolean"
    && typeof value.active === "boolean"
    && typeof value.provider_label === "string"
    && isNullableString(value.provider_email)
    && isNullableString(value.plan_type);
}

export function isProviderSettingsResponse(value: unknown): value is ProviderSettingsResponse {
  return isRecord(value)
    && (value.active_provider === null || isProviderMethod(value.active_provider))
    && Array.isArray(value.connections)
    && value.connections.every(isProviderConnection);
}

export function isAgentIdentity(value: unknown): value is AgentIdentity {
  return isRecord(value)
    && typeof value.name === "string"
    && typeof value.soul === "string"
    && typeof value.core_soul === "string"
    && typeof value.effective_soul === "string"
    && typeof value.identity_path === "string"
    && typeof value.soul_path === "string"
    && typeof value.updated_at === "string";
}

function isAgentAsset(value: unknown): value is AgentAsset {
  return isRecord(value)
    && (value.kind === "memory" || value.kind === "skill")
    && typeof value.name === "string"
    && typeof value.title === "string"
    && isNullableString(value.description)
    && typeof value.content === "string"
    && typeof value.path === "string"
    && typeof value.built_in === "boolean"
    && typeof value.editable === "boolean";
}

export function isAgentAssetSettingsResponse(value: unknown): value is AgentAssetSettingsResponse {
  return isRecord(value)
    && Array.isArray(value.memories)
    && value.memories.every(isAgentAsset)
    && Array.isArray(value.skills)
    && value.skills.every(isAgentAsset);
}

function isEnvironment(value: unknown): value is Record<string, string> {
  return isRecord(value) && Object.values(value).every((item) => typeof item === "string");
}

function isMCPServerSettings(value: unknown): value is MCPServerSettings {
  return isRecord(value)
    && typeof value.name === "string"
    && typeof value.display_name === "string"
    && typeof value.description === "string"
    && (value.transport === "http" || value.transport === "stdio")
    && isNullableString(value.command)
    && isStringArray(value.args)
    && isNullableString(value.url)
    && isStringArray(value.tool_allowlist)
    && isStringArray(value.forwarded_environment)
    && isEnvironment(value.environment)
    && isNullableString(value.source_url)
    && isNullableString(value.warning)
    && typeof value.enabled === "boolean"
    && typeof value.preset === "boolean"
    && (value.command_available === null || typeof value.command_available === "boolean");
}

export function isMCPSettingsResponse(value: unknown): value is MCPSettingsResponse {
  return isRecord(value)
    && Array.isArray(value.servers)
    && value.servers.every(isMCPServerSettings);
}

function isCapabilityGroup(value: unknown): value is CapabilityGroup {
  return isRecord(value)
    && typeof value.id === "string"
    && typeof value.name === "string"
    && typeof value.description === "string"
    && ["enabled", "setup_required", "limited", "disabled"].includes(String(value.state))
    && typeof value.state_label === "string"
    && isStringArray(value.tools)
    && isNullableString(value.note);
}

function isMCPServerCapability(value: unknown): value is MCPServerCapability {
  return isRecord(value)
    && typeof value.name === "string"
    && typeof value.display_name === "string"
    && typeof value.enabled === "boolean"
    && (value.transport === "http" || value.transport === "stdio")
    && isStringArray(value.tools);
}

export function isCapabilitySettingsResponse(value: unknown): value is CapabilitySettingsResponse {
  if (!isRecord(value) || !isRecord(value.web_search)) return false;
  return isStringArray(value.enabled_toolsets)
    && isStringArray(value.disabled_toolsets)
    && Array.isArray(value.groups)
    && value.groups.every(isCapabilityGroup)
    && Array.isArray(value.mcp_servers)
    && value.mcp_servers.every(isMCPServerCapability)
    && value.web_search.provider === "brave"
    && typeof value.web_search.provider_label === "string"
    && typeof value.web_search.configured === "boolean"
    && typeof value.web_search.setup_url === "string"
    && value.web_search.key_storage === "encrypted_local";
}
