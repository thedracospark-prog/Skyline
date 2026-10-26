/* Skyline service worker — caches the app shell for offline use.
   API responses are NOT cached here; the app keeps its own last-synced
   fleet snapshot and an offline edit queue in localStorage.
   Strategy (v2): network-first for navigations and app files so a new
   deployed version is picked up on the next load, with the cache as
   the offline fallback. */
const CACHE = "skyline-shell-v5";
const SHELL = ["/", "/index.html", "/style.css", "/app.js", "/manifest.json",
  "/icons/icon-192.png", "/icons/icon-512.png", "/icons/apple-touch-icon.png",
  "/icons/favicon-32.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL))
    .then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(
    keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/api/")) return; // network only; app handles queue
  if (e.request.method !== "GET") return;
  e.respondWith(
    fetch(e.request).then((res) => {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(e.request, copy));
      return res;
    }).catch(() => caches.match(e.request)
      .then((hit) => hit || caches.match("/index.html")))
  );
});
