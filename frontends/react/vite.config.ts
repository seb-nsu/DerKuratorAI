// Dev server proxies /api to the API (uvicorn app.api.main:app on port 8000); the production build is served by the
// API process itself (scripts/serve.py with FRONTEND=react).
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": "http://127.0.0.1:8000" } },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 6000 },
});
