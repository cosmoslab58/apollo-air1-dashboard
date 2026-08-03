const CACHE_NAME = "apollo-air1-shell-v7";
const SHELL_FILES = [
  "/static/fonts/instrument-sans.woff2",
  "/static/fonts/martian-mono.woff2",
  "/",
  "/forecast",
  // Canonical URL, not /technical -- precaching the old path would store a 301
  // under it and leave the real page uncached. /technical still redirects for
  // anything already bookmarked.
  "/outdoor",
  "/indoor",
  "/static/style.css",
  "/static/aqi.js",
  "/static/common.js",
  "/static/chart.js",
  "/static/dashboard.js",
  "/static/forecast.js",
  "/static/technical.js",
  "/static/indoor.js",
  "/static/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // Live sensor data: always go to the network, never serve from cache.
  if (url.pathname.startsWith("/api/")) {
    event.respondWith(fetch(event.request));
    return;
  }

  // App shell: network-first, so a new deploy is visible on the very next
  // load instead of needing a hard refresh to bypass a stale cached copy.
  // Cache is only a fallback for when the network request actually fails
  // (offline), not a way to skip the network on a normal load.
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        }
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});
