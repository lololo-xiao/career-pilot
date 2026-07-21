import { Browser } from "@capacitor/browser";
import { Capacitor } from "@capacitor/core";
import { Directory, Filesystem } from "@capacitor/filesystem";
import { Share } from "@capacitor/share";


export function isNativeApp(): boolean {
  return Capacitor.isNativePlatform();
}

export async function openExternalUrl(url: string): Promise<boolean> {
  if (!isNativeApp()) return false;
  await Browser.open({
    url,
    presentationStyle: "popover",
    toolbarColor: "#f4f0e8",
  });
  return true;
}

export async function closeExternalBrowser(): Promise<void> {
  if (!isNativeApp()) return;
  await Browser.close().catch(() => undefined);
}

function safeExportFilename(filename: string): string {
  const sanitized = filename
    .normalize("NFKC")
    .replaceAll(/[^A-Za-z0-9._-]+/g, "-")
    .replaceAll(/^-+|-+$/g, "")
    .slice(0, 120);
  return sanitized || "careerpilot-export";
}

function blobAsBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("File could not be read"));
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") {
        reject(new Error("File could not be encoded"));
        return;
      }
      resolve(result.slice(result.indexOf(",") + 1));
    };
    reader.readAsDataURL(blob);
  });
}

export async function shareNativeBlob(
  blob: Blob,
  filename: string,
  title = "CareerPilot export",
): Promise<boolean> {
  if (!isNativeApp()) return false;
  const safeFilename = safeExportFilename(filename);
  const written = await Filesystem.writeFile({
    path: `exports/${Date.now()}-${safeFilename}`,
    data: await blobAsBase64(blob),
    directory: Directory.Cache,
    recursive: true,
  });
  await Share.share({
    title,
    dialogTitle: "Save or share CareerPilot export",
    files: [written.uri],
  });
  return true;
}
