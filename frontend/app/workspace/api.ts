import { API_BASE_URL } from "../api-base-url";
import { shareNativeBlob } from "../native-platform";

export { API_BASE_URL };

export async function apiRequest<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers,
    credentials: "include",
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as
      | { detail?: string }
      | null;
    throw new Error(payload?.detail ?? `${response.status} ${response.statusText}`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export async function openArtifact(artifactId: string): Promise<void> {
  const response = await fetch(
    `${API_BASE_URL}/api/v1/artifacts/${artifactId}/content`,
    { credentials: "include" },
  );
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as
      | { detail?: string }
      | null;
    throw new Error(payload?.detail ?? "Artifact could not be opened");
  }
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const encodedFilename = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const plainFilename = disposition.match(/filename="?([^";]+)"?/i)?.[1];
  const fallbackFilename = plainFilename ?? `careerpilot-${artifactId}`;
  let filename = encodedFilename ?? fallbackFilename;
  if (encodedFilename) {
    try {
      filename = decodeURIComponent(encodedFilename);
    } catch {
      filename = fallbackFilename;
    }
  }
  if (await shareNativeBlob(blob, filename, "CareerPilot document")) return;
  const blobUrl = URL.createObjectURL(blob);
  window.open(blobUrl, "_blank", "noopener,noreferrer");
  window.setTimeout(() => URL.revokeObjectURL(blobUrl), 60_000);
}
