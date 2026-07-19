import type { CodexLoginStartResponse } from "./types";


const CODEX_ATTEMPT_KEY_PREFIX = "careerpilot_codex_login_attempt:";

export interface SessionStorageLike {
  getItem: (key: string) => string | null;
  removeItem: (key: string) => void;
  setItem: (key: string, value: string) => void;
}

export interface CodexAttemptStorageBinding {
  readonly attempt: CodexLoginStartResponse | null;
  readonly key: string;
  readonly userId: string;
}

export interface CodexAttemptStorageLifecycle {
  bind: (userId: string | null | undefined) => CodexAttemptStorageBinding | null;
  persist: (
    binding: CodexAttemptStorageBinding | null,
    attempt: CodexLoginStartResponse | null,
  ) => boolean;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isCodexLoginStartResponse(
  value: unknown,
): value is CodexLoginStartResponse {
  return isRecord(value)
    && typeof value.attempt_id === "string"
    && value.attempt_id.length > 0
    && typeof value.verification_url === "string"
    && value.verification_url.length > 0
    && typeof value.user_code === "string"
    && value.user_code.length > 0
    && typeof value.expires_at === "number"
    && Number.isFinite(value.expires_at);
}

export function codexAttemptStorageKey(
  userId: string | null | undefined,
): string | null {
  return typeof userId === "string" && userId.length > 0
    ? `${CODEX_ATTEMPT_KEY_PREFIX}${userId}`
    : null;
}

export function createCodexAttemptStorageLifecycle(
  storage: SessionStorageLike,
  nowSeconds: () => number = () => Date.now() / 1000,
): CodexAttemptStorageLifecycle {
  let activeBinding: CodexAttemptStorageBinding | null = null;

  return {
    bind(userId) {
      const key = codexAttemptStorageKey(userId);
      if (!key || !userId) {
        activeBinding = null;
        return null;
      }

      let attempt: CodexLoginStartResponse | null = null;
      try {
        const rawAttempt = storage.getItem(key);
        if (rawAttempt) {
          const parsed = JSON.parse(rawAttempt) as unknown;
          if (
            isCodexLoginStartResponse(parsed)
            && parsed.expires_at > nowSeconds()
          ) {
            attempt = parsed;
          } else {
            storage.removeItem(key);
          }
        }
      } catch {
        try {
          storage.removeItem(key);
        } catch {
          // Storage can be unavailable without invalidating the in-tab connection.
        }
      }

      const binding = { attempt, key, userId };
      activeBinding = binding;
      return binding;
    },
    persist(binding, attempt) {
      if (!binding || binding !== activeBinding) return false;
      try {
        if (attempt) {
          if (!isCodexLoginStartResponse(attempt)) return false;
          storage.setItem(binding.key, JSON.stringify(attempt));
        } else {
          storage.removeItem(binding.key);
        }
        return true;
      } catch {
        return false;
      }
    },
  };
}
