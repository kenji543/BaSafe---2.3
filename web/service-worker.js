const CACHE_VERSION = "geosafe-shell-v46";
const INFO_CACHE = "geosafe-info-v46";
const APP_SHELL = [
  "/",
  "/map",
  "/methodology",
  "/data-sources",
  "/limitations",
  "/about",
  "/privacy",
  "/offline",
  "/site.css?v=0.6.7",
  "/styles.css?v=0.6.8",
  "/landing.js?v=0.6.1",
  "/app.js?v=0.6.12",
  "/methodology.js?v=0.5.13",
  "/info.js?v=0.5.16",
  "/pwa.js?v=0.6.2",
  "/manifest.webmanifest",
  "/icons/basafe-logo-256.png",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/icon-maskable-512.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_VERSION).then((cache) => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => ![CACHE_VERSION, INFO_CACHE].includes(key)).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

const informationalRoutes = new Set(["/", "/methodology", "/data-sources", "/limitations", "/about", "/privacy", "/offline"]);

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith("/api/") || url.pathname.includes("/report")) {
    event.respondWith(fetch(request));
    return;
  }

  if (informationalRoutes.has(url.pathname)) {
    event.respondWith(
      caches.open(INFO_CACHE).then(async (cache) => {
        const cached = await cache.match(request);
        try {
          const response = await fetch(request);
          if (response.ok) await cache.put(request, response.clone());
          return response;
        } catch {
          if (cached) return cached;
          const shellCache = await caches.open(CACHE_VERSION);
          return (await shellCache.match(url.pathname)) || (await shellCache.match("/offline"));
        }
      })
    );
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => cached || fetch(request).catch(() => {
      if (request.mode === "navigate") return caches.match("/offline");
      return new Response("Offline", {status: 503, headers: {"Content-Type": "text/plain; charset=utf-8"}});
    }))
  );
});
