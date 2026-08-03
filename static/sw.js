const CACHE_NAME = "apollo-air1-shell-v8";
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
  //
  // "Network-first" was not enough on its own: a plain fetch() still consults
  // the HTTP cache, and Cloudflare rewrites this origin's `Cache-Control:
  // no-cache` on /static/* into `max-age=14400`. So the network step was
  // legitimately answering from a 4-hour-old copy -- new HTML (never cached,
  // it is dynamic) against stale CSS/JS, which is a worse failure than a plain
  // stale page because the two halves disagree. Measured on a live deploy: the
  // page ran a chart.js four hours older than the markup that called into it.
  //
  // Only the code assets are forced past the HTTP cache. Fonts and icons are
  // the bulk of the bytes and effectively immutable, so they keep normal
  // caching; style.css/*.js are a few tens of KB and are exactly what changes
  // on a deploy. Navigations need no help here -- the HTML is dynamic, so
  // Cloudflare passes it through uncached -- and must be left alone regardless:
  // re-constructing a request whose mode is "navigate" with a non-empty init
  // throws, which would take out every page load rather than just staleness.
  // The offline fallback below is unaffected either way: cache.put still keys
  // on the original request.
  const request = /\.(?:css|js)$/.test(url.pathname)
    ? new Request(event.request, { cache: "no-store" })
    : event.request;
  event.respondWith(
    fetch(request)
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
