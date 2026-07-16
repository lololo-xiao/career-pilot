import type { Metadata } from "next";

import "./globals.css";
import "./companion.css";
import "./workspace/workspace.css";


export const metadata: Metadata = {
  title: "CareerPilot — Your career companion",
  description:
    "A thoughtful AI partner for the job search, grounded in your real career evidence.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" data-scroll-behavior="smooth">
      <body>{children}</body>
    </html>
  );
}
