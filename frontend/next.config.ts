import type { NextConfig } from "next";

const staticExport = process.env.CAREERPILOT_STATIC_EXPORT === "true";

const nextConfig: NextConfig = {
  output: staticExport ? "export" : "standalone",
  trailingSlash: staticExport,
  images: { unoptimized: staticExport },
  poweredByHeader: false,
  turbopack: {
    root: process.cwd(),
  },
};

export default nextConfig;
