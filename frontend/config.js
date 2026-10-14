// Runtime API-base configuration for the dashboard (static site; no build step).
//
// This file is the single, documented override point for where the UI sends its /api/v1 calls.
// Load it before app.js / lab.js. To point the UI at a specific backend without editing code,
// set window.DITTO_API_BASE here (or inject it from your host before this script runs):
//
//   window.DITTO_API_BASE = "https://ditto-debugger1.onrender.com";
//
// Behaviour when DITTO_API_BASE is left unset (see resolveApiBase in app.js / lab.js):
//   * Served from localhost / 127.0.0.1  -> "" (empty base, i.e. relative, same-origin) so a
//     locally-run API (python -m uvicorn ... --host 127.0.0.1 --port 8000) keeps working.
//   * Served from any other host          -> the deployed Render backend.
//
// Keep secrets out of this file: it is public, static JavaScript.
(function () {
  if (typeof window.DITTO_API_BASE === "string") return; // an override was already injected
  // Default override target for the deployed split-origin setup (static frontend -> backend API).
  // Comment this out to rely purely on hostname auto-detection.
  window.DITTO_API_BASE = "https://ditto-debugger1.onrender.com";
})();
