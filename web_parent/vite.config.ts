import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served by FastAPI under /parent (StaticFiles on web_parent/dist).
export default defineConfig({
  plugins: [react()],
  base: "/parent/",
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    // dev proxy so `npm run dev` can talk to the API on :8090
    proxy: { "/api": "http://127.0.0.1:8090" },
  },
});
