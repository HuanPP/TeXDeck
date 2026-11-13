import { cpSync, createReadStream, existsSync, statSync } from "node:fs";
import { join, normalize, resolve } from "node:path";
import { defineConfig, loadEnv, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// pdf.js needs its CMaps to draw CID-keyed fonts (Fandol, many Chinese and
// Japanese PDFs) and its standard font data for unembedded base fonts. They
// ship in pdfjs-dist; serve them at /pdfjs/ in dev and copy them into the build.
const PDFJS_ASSETS = ["cmaps", "standard_fonts"];
function pdfjsAssets(): Plugin {
  const root = resolve("node_modules/pdfjs-dist");
  let outDir = "dist";
  return {
    name: "texdeck-pdfjs-assets",
    configResolved(config) { outDir = resolve(config.root, config.build.outDir); },
    configureServer(server) {
      server.middlewares.use("/pdfjs", (req, res, next) => {
        const path = normalize(join(root, decodeURIComponent((req.url ?? "").split("?")[0])));
        const ok = PDFJS_ASSETS.some((d) => path.startsWith(join(root, d))) && existsSync(path) && statSync(path).isFile();
        if (!ok) return next();
        createReadStream(path).pipe(res);
      });
    },
    writeBundle() {
      for (const d of PDFJS_ASSETS) cpSync(join(root, d), join(outDir, "pdfjs", d), { recursive: true });
    },
  };
}

export default defineConfig(({ mode }) => {
  // Backend origin is overridable (e.g. VITE_BACKEND_ORIGIN=http://host:9000)
  // via .env files or the shell environment.
  const env = loadEnv(mode, process.cwd(), "");
  const backend = env.VITE_BACKEND_ORIGIN || "http://localhost:8000";
  return {
    plugins: [react(), tailwindcss(), pdfjsAssets()],
    server: {
      proxy: {
        "/api": {
          target: backend,
          changeOrigin: true,
        },
        "/ws": {
          target: backend.replace(/^http/, "ws"),
          ws: true,
          changeOrigin: true,
        },
      },
    },
  };
});
