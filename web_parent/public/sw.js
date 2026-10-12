// Tellybear parent PWA service worker.
// Caches the APP SHELL ONLY (never API data — never another family's data).
const SHELL = "tellybear-shell-v16";
const SHELL_ASSETS = ["/parent/", "/parent/index.html", "/parent/manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k.startsWith("tellybear-shell-") && k !== SHELL).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // Never cache API responses (data isolation): always go to network.
  if (url.pathname.startsWith("/api/")) return;
  if (e.request.method !== "GET") return;
  // Navigations (index.html): network-first so a new deploy's JS is picked up right away; the
  // cached shell is only the offline fallback.
  if (e.request.mode === "navigate") {
    e.respondWith(
      fetch(e.request).then((res) => {
        if (res && res.status === 200) {
          const copy = res.clone();
          caches.open(SHELL).then((c) => c.put("/parent/", copy));
        }
        return res;
      }).catch(() => caches.match("/parent/"))
    );
    return;
  }
  // Static build assets (content-hashed): cache-first, filled from the network on a miss.
  e.respondWith(
    caches.match(e.request).then((cached) => {
      const network = fetch(e.request).then((res) => {
        if (res && res.status === 200 && url.pathname.startsWith("/parent/")) {
          const copy = res.clone();
          caches.open(SHELL).then((c) => c.put(e.request, copy));
        }
        return res;
      }).catch(() => cached);
      return cached || network;
    })
  );
});
