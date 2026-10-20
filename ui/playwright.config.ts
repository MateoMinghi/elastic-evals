import { defineConfig } from "@playwright/test";

// Runs against a live API serving the built UI (python -m elastic_evals serve), base URL from EE_UI_URL.
export default defineConfig({
  testDir: "tests",
  timeout: 30_000,
  use: {
    baseURL: process.env.EE_UI_URL || "http://localhost:8000",
    launchOptions: { executablePath: process.env.PW_CHROMIUM || undefined },
    screenshot: "only-on-failure",
  },
});
