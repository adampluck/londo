/* Service worker: app shell cached for instant/offline loads,
   events API network-first with cached fallback (offline shows the
   last events you saw). Shared by every site built from web/; the
   per-site config.js gives each its own cache name — both sites live
   on the same github.io origin, where Cache Storage is origin-wide. */

importScripts("config.js");

const SITE = (self.LONDO_CONFIG && self.LONDO_CONFIG.SITE) || {};
const CACHE = (SITE.id || "londo") + "-v12";
const SHELL = [
  "./",
  "index.html",
  "styles.css",
  "app.js",
  "config.js",
  "manifest.webmanifest",
  "icons/icon-192.png",
  "icons/icon-512.png",
  ...(SITE.shellExtras || []),
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches
      .open(CACHE)
      .then((c) => c.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const { request } = e;
  if (request.method !== "GET") return;

  // analytics must hit the network every time, never the cache
  if (/goatcounter\.com|gc\.zgo\.at/.test(request.url)) return;

  // events API: fresh when online, cached events when not
  if (request.url.includes("/rest/v1/")) {
    e.respondWith(
      fetch(request)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(request, copy));
          return res;
        })
        .catch(() => caches.match(request))
    );
    return;
  }

  // everything else (shell, fonts, images): cache-first, refresh behind
  e.respondWith(
    caches.match(request).then((cached) => {
      const refresh = fetch(request)
        .then((res) => {
          if (res.ok || res.type === "opaque") {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(request, copy));
          }
          return res;
        })
        .catch(() => cached);
      return cached || refresh;
    })
  );
});
