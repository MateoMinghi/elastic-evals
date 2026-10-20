import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` proxies the API on :8000; `npm run build` emits dist/, which the API serves at /.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/v1": "http://localhost:8000", "/healthz": "http://localhost:8000" } },
});
