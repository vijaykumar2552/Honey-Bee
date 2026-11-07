import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served by Flask at /app/ in production (honeychain/app.py serves frontend/dist).
// In development `npm run dev` proxies API calls to the Flask backend.
export default defineConfig({
  base: "/app/",
  plugins: [react()],
  server: { proxy: { "/api": "http://localhost:5000" } },
  build: { outDir: "dist", sourcemap: false },
});
