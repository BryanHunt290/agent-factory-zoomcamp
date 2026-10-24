// Vite configuration for the Agent Factory dashboard (docs/frontend.md).
//
// Everything here runs in Node on the developer's machine, never in the browser: nothing in this file is
// bundled. The browser talks to one origin, this server; requests for the API are proxied to the API on
// loopback, so the API needs no CORS settings and keeps refusing foreign Host headers.
import { defineConfig } from "vite";

const LOOPBACK = new Set(["127.0.0.1", "localhost", "[::1]"]);

/** The API to proxy to: AGENT_FACTORY_API_ORIGIN, loopback only (default http://127.0.0.1:8000). */
function apiOrigin() {
  const origin = process.env.AGENT_FACTORY_API_ORIGIN || "http://127.0.0.1:8000";
  const url = new URL(origin);
  if (url.protocol !== "http:" || !LOOPBACK.has(url.hostname) || url.pathname !== "/" || url.search || url.hash) {
    throw new Error(`AGENT_FACTORY_API_ORIGIN must be http://127.0.0.1:<port> (loopback only), not ${origin}`);
  }
  return url.origin;
}

function proxy() {
  const target = apiOrigin();
  // The API refuses Host headers that are not loopback names; changeOrigin presents its own.
  const route = { target, changeOrigin: true, secure: false, ws: false };
  return { "/api/": route, "/healthz": route, "/readyz": route };
}

// The production bundle is served with a strict policy: only this origin's own scripts, styles, and API.
const CSP = [
  "default-src 'none'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self' data:",
  "font-src 'self'",
  "connect-src 'self'",
  "manifest-src 'self'",
  "base-uri 'none'",
  "form-action 'self'",
  "frame-ancestors 'none'",
  "object-src 'none'",
].join("; ");

// The dev server injects module CSS as <style> elements and reloads over a WebSocket.
const DEV_CSP = CSP.replace("style-src 'self'", "style-src 'self' 'unsafe-inline'").replace(
  "connect-src 'self'",
  "connect-src 'self' ws://127.0.0.1:* ws://localhost:*",
);

const securityHeaders = (csp) => ({
  "Content-Security-Policy": csp,
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
  "X-Frame-Options": "DENY",
  "Cross-Origin-Opener-Policy": "same-origin",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
  "Cache-Control": "no-store",
});

export default defineConfig({
  oxc: { jsx: { runtime: "automatic", importSource: "react" } },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: proxy(),
    headers: securityHeaders(DEV_CSP),
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
    strictPort: true,
    proxy: proxy(),
    headers: securityHeaders(CSP),
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    target: "es2022",
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
  },
});
