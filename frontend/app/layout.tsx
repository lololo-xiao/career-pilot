import type { Metadata, Viewport } from "next";

import { NativeAppShell } from "./native-app-shell";
import "./globals.css";
import "./companion.css";
import "./workspace/workspace.css";


export const metadata: Metadata = {
  title: "CareerPilot — Your career companion",
  description:
    "A thoughtful AI partner for the job search, grounded in your real career evidence.",
};

export const viewport: Viewport = {
  colorScheme: "light",
  initialScale: 1,
  themeColor: "#f4f0e8",
  viewportFit: "cover",
  width: "device-width",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" data-scroll-behavior="smooth">
      <body><NativeAppShell>{children}</NativeAppShell></body>
    </html>
  );
}
