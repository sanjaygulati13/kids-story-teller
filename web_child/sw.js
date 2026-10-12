// Child player service worker. Goal: a Wi-Fi blip mid-bedtime must not blank the page.
// Strategy:
//   - the player shell (HTML/navigations): stale-while-revalidate — instant from cache, refreshed
//     in the background so a new deploy still propagates.
//   - catalog/profile API GETs: network-first, fall back to the last cached copy when offline.
//   - audio (/audio/...): NEVER cached here — it's range-streamed and large; let the network/browser
//     handle it (already-played files may sit in the HTTP cache anyway).
const CACHE = 'tellybear-child-v2';

self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(
  caches.keys().then((ks) => Promise.all(ks.filter((k) => k.startsWith('tellybear-child-') && k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim())
));

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  if (url.pathname.includes('/audio/')) return;  // never intercept audio streaming

  // shell / navigations → stale-while-revalidate
  if (req.mode === 'navigate' || url.pathname.startsWith('/play')) {
    e.respondWith((async () => {
      const cached = await caches.match(req, { ignoreSearch: true });
      const fetching = fetch(req).then((res) => {
        if (res && res.ok) {
          const copy = res.clone();  // clone NOW, before the page consumes the body
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      }).catch(() => cached);
      return cached || fetching;
    })());
    return;
  }

  // catalog / profile API → network-first with cache fallback
  if (url.pathname.includes('/api/child/catalog/') || url.pathname.includes('/api/child/profile/')) {
    e.respondWith((async () => {
      try {
        const res = await fetch(req);
        if (res && res.ok) (await caches.open(CACHE)).put(req, res.clone());
        return res;
      } catch (err) {
        const cached = await caches.match(req, { ignoreSearch: true });
        if (cached) return cached;
        throw err;
      }
    })());
  }
});
