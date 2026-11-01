// Service worker: makes the app installable, and shows a short notice instead of
// the browser's error page when the server can't be reached (e.g. away from home).
// Nothing is cached: chats always come from the server.
const OFFLINE = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Helper – offline</title>
<style>
  :root { color-scheme: light dark; }
  body { margin: 0; min-height: 100dvh; display: grid; place-items: center; padding: 16px; box-sizing: border-box;
         font: 16px/1.5 system-ui, sans-serif; text-align: center; background: #f6f7f9; color: #1d2330; }
  @media (prefers-color-scheme: dark) { body { background: #14171c; color: #e4e7ec; } }
  p { color: #667085; }
  button { font: inherit; font-weight: 600; padding: 10px 20px; border: 0; border-radius: 8px; background: #2f5bd3; color: #fff; }
</style></head>
<body><div><h1>Can't reach the helper</h1>
<p>The server isn't answering. Check your connection, or whether the helper is running, and try again.</p>
<button onclick="location.reload()">Try again</button></div></body></html>`;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  if (e.request.mode !== "navigate" || e.request.method !== "GET") return;
  e.respondWith(fetch(e.request).catch(() =>
    new Response(OFFLINE, { headers: { "Content-Type": "text/html; charset=utf-8" } })));
});
