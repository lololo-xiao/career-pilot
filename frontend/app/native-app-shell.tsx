"use client";

import { Capacitor } from "@capacitor/core";
import { StatusBar, Style } from "@capacitor/status-bar";
import { FormEvent, ReactNode, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import {
  NATIVE_API_URL_STORAGE_KEY,
  resolveApiBaseUrl,
  saveNativeApiBaseUrl,
} from "./api-base-url";
import { isNativeApp, openExternalUrl } from "./native-platform";
import { getNativeViewportState } from "./native-viewport";


function normalizedBackendUrl(value: string): string {
  const url = new URL(value.trim());
  const localDevelopment =
    url.protocol === "http:"
    && (url.hostname === "127.0.0.1" || url.hostname === "localhost");
  if (url.protocol !== "https:" && !localDevelopment) {
    throw new Error("Use an HTTPS backend. Plain HTTP is allowed only for a local simulator.");
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error("Enter only the backend origin, without credentials, query text, or a fragment.");
  }
  if (url.pathname !== "/") {
    throw new Error("Enter the backend origin without an extra path.");
  }
  return url.origin;
}

function hasFocusedTextControl(): boolean {
  const activeElement = document.activeElement;
  if (activeElement instanceof HTMLTextAreaElement) return true;
  if (activeElement instanceof HTMLElement && activeElement.isContentEditable) return true;
  if (!(activeElement instanceof HTMLInputElement)) return false;
  return !["button", "checkbox", "color", "file", "hidden", "image", "radio", "range", "reset", "submit"].includes(
    activeElement.type,
  );
}

function NativeBackendSetup({ onReady }: { onReady: () => void }) {
  const [backendUrl, setBackendUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  async function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setChecking(true);
    setError(null);
    try {
      const normalized = normalizedBackendUrl(backendUrl);
      const response = await fetch(`${normalized}/health`, {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      const payload = (await response.json().catch(() => null)) as
        | { status?: string }
        | null;
      if (!response.ok || payload?.status !== "ok") {
        throw new Error("That server did not return a healthy CareerPilot API.");
      }
      saveNativeApiBaseUrl(normalized);
      onReady();
    } catch (caught) {
      setError(
        caught instanceof TypeError
          ? "CareerPilot could not reach that server. Check HTTPS and allow capacitor://localhost in FRONTEND_ORIGINS."
          : caught instanceof Error
            ? caught.message
            : "CareerPilot could not validate that server.",
      );
    } finally {
      setChecking(false);
    }
  }

  return (
    <main className="native-setup-shell">
      <section className="native-setup-card">
        <div className="native-setup-mark" aria-hidden="true">CP</div>
        <span className="native-setup-eyebrow">iPhone setup</span>
        <h1>Connect your private CareerPilot runtime.</h1>
        <p>
          The iOS app carries the full interface. Your AI runtime, encrypted provider
          connection, career database, document tools, and approvals stay on your own
          CareerPilot server.
        </p>
        <form onSubmit={connect}>
          <label htmlFor="careerpilot-native-backend">Private backend URL</label>
          <input
            autoCapitalize="none"
            autoComplete="url"
            autoCorrect="off"
            id="careerpilot-native-backend"
            inputMode="url"
            onChange={(event) => setBackendUrl(event.target.value)}
            placeholder="https://careerpilot.example.com"
            required
            type="url"
            value={backendUrl}
          />
          <button disabled={checking} type="submit">
            {checking ? "Checking secure connection…" : "Connect securely"}
          </button>
        </form>
        {error ? <p className="native-setup-error" role="alert">{error}</p> : null}
        <p className="native-setup-warning">
          Do not connect this app to an unprotected public CareerPilot server. The
          current runtime is single-user and must sit behind private network access or
          an authentication gateway. <Link href="/privacy">Review privacy details.</Link>
        </p>
      </section>
    </main>
  );
}

export function NativeAppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [platform, setPlatform] = useState<"checking" | "native" | "web">("checking");

  useEffect(() => {
    const timer = window.setTimeout(
      () => setPlatform(isNativeApp() ? "native" : "web"),
      0,
    );
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    const native = platform === "native";
    if (!native) return;
    const root = document.documentElement;
    const visualViewport = window.visualViewport;
    let animationFrame: number | null = null;
    let viewportBaselineHeight = window.innerHeight;

    const syncViewport = () => {
      if (animationFrame !== null) window.cancelAnimationFrame(animationFrame);
      animationFrame = window.requestAnimationFrame(() => {
        animationFrame = null;
        const textControlFocused = hasFocusedTextControl();
        if (!textControlFocused) {
          viewportBaselineHeight = window.innerHeight;
        } else {
          viewportBaselineHeight = Math.max(viewportBaselineHeight, window.innerHeight);
        }
        const viewportState = getNativeViewportState({
          hasFocusedTextControl: textControlFocused,
          layoutHeight: viewportBaselineHeight,
          visualHeight: visualViewport?.height,
          visualOffsetTop: visualViewport?.offsetTop,
        });
        root.style.setProperty("--native-viewport-height", `${viewportState.height}px`);
        root.style.setProperty("--native-viewport-offset-top", `${viewportState.offsetTop}px`);
        root.classList.toggle("native-text-entry-focused", textControlFocused);
        root.classList.toggle("native-keyboard-visible", viewportState.keyboardVisible);
      });
    };

    root.classList.add("capacitor-native");
    syncViewport();
    void StatusBar.setOverlaysWebView({ overlay: false });
    void StatusBar.setStyle({ style: Style.Dark });

    const openExternalLink = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0) return;
      const target = event.target;
      if (!(target instanceof Element)) return;
      const anchor = target.closest<HTMLAnchorElement>('a[target="_blank"]');
      if (!anchor || !/^https?:$/i.test(new URL(anchor.href).protocol)) return;
      event.preventDefault();
      void openExternalUrl(anchor.href);
    };
    document.addEventListener("click", openExternalLink);
    document.addEventListener("focusin", syncViewport);
    document.addEventListener("focusout", syncViewport);
    window.addEventListener("resize", syncViewport);
    visualViewport?.addEventListener("resize", syncViewport);
    visualViewport?.addEventListener("scroll", syncViewport);
    return () => {
      if (animationFrame !== null) window.cancelAnimationFrame(animationFrame);
      root.classList.remove(
        "capacitor-native",
        "native-keyboard-visible",
        "native-text-entry-focused",
      );
      root.style.removeProperty("--native-viewport-height");
      root.style.removeProperty("--native-viewport-offset-top");
      document.removeEventListener("click", openExternalLink);
      document.removeEventListener("focusin", syncViewport);
      document.removeEventListener("focusout", syncViewport);
      window.removeEventListener("resize", syncViewport);
      visualViewport?.removeEventListener("resize", syncViewport);
      visualViewport?.removeEventListener("scroll", syncViewport);
    };
  }, [platform]);

  if (platform === "checking") {
    return (
      <main className="native-bootstrap" aria-label="Opening CareerPilot">
        <span aria-hidden="true">CP</span>
      </main>
    );
  }
  if (platform === "native" && !resolveApiBaseUrl() && pathname !== "/privacy") {
    return <NativeBackendSetup onReady={() => window.location.reload()} />;
  }
  return children;
}

export function clearNativeBackend(): void {
  if (!Capacitor.isNativePlatform()) return;
  window.localStorage.removeItem(NATIVE_API_URL_STORAGE_KEY);
  window.location.reload();
}

export function NativeBackendControl() {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => setVisible(isNativeApp()), 0);
    return () => window.clearTimeout(timer);
  }, []);

  if (!visible) return null;
  return (
    <button
      className="header-action"
      onClick={() => {
        if (window.confirm("Disconnect this iPhone from its current CareerPilot server?")) {
          clearNativeBackend();
        }
      }}
      type="button"
    >
      Change iOS server
    </button>
  );
}
