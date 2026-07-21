const COMPILED_API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.trim().replace(/\/$/, "") ?? "";

export const NATIVE_API_URL_STORAGE_KEY = "careerpilot.native.api-url";

function storedNativeApiBaseUrl(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage
      .getItem(NATIVE_API_URL_STORAGE_KEY)
      ?.trim()
      .replace(/\/$/, "") ?? "";
  } catch {
    return "";
  }
}

export function resolveApiBaseUrl(): string {
  return COMPILED_API_BASE_URL || storedNativeApiBaseUrl();
}

export function saveNativeApiBaseUrl(value: string): void {
  window.localStorage.setItem(
    NATIVE_API_URL_STORAGE_KEY,
    value.trim().replace(/\/$/, ""),
  );
}

export const API_BASE_URL = resolveApiBaseUrl();
