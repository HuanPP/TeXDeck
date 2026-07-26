import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig(({ mode }) => {
  // Backend origin is overridable (e.g. VITE_BACKEND_ORIGIN=http://host:9000)
  // via .env files or the shell environment.
  const env = loadEnv(mode, process.cwd(), "");
  const backend = env.VITE_BACKEND_ORIGIN || "http://localhost:8000";
  return {
    plugins: [react(), tailwindcss()],
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
