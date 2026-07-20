import type { CapacitorConfig } from "@capacitor/cli";

const config: CapacitorConfig = {
  appId: "com.careerpilot.app",
  appName: "CareerPilot",
  webDir: "out",
  loggingBehavior: "debug",
  ios: {
    backgroundColor: "#f4f0e8",
    contentInset: "automatic",
    preferredContentMode: "mobile",
    scheme: "CareerPilot",
  },
  plugins: {
    StatusBar: {
      overlaysWebView: false,
      style: "DARK",
    },
  },
};

export default config;
