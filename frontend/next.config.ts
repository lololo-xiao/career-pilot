import type { NextConfig } from "next";

const staticExport = process.env.CAREERPILOT_STATIC_EXPORT === "true";
const staticBuildId = process.env.CAREERPILOT_BUILD_ID;

if (staticExport && !staticBuildId) {
  throw new Error(
    "Static releases must use scripts.static_ui_release to set CAREERPILOT_BUILD_ID",
  );
}

const nextConfig: NextConfig = {
  generateBuildId: staticExport ? async () => staticBuildId! : undefined,
  output: staticExport ? "export" : "standalone",
  trailingSlash: staticExport,
  images: { unoptimized: staticExport },
  poweredByHeader: false,
  turbopack: {
    root: process.cwd(),
  },
};

export default nextConfig;
