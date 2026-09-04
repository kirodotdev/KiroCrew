import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Public-facing landing site. The repo is currently private, so GitHub Pages
// serves it at the root of a *.pages.github.io subdomain. A relative base keeps
// assets resolving at that root and also survives a move to a /kirocrew/ subpath
// if the repo goes public later.
export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss()],
  server: { port: 3000 },
  build: { outDir: "dist" },
  test: { environment: "jsdom", setupFiles: ["./tests/setup.ts"], globals: true },
});
