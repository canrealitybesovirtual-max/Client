/* TradeComply service worker: the app shell works offline; API calls go to the network first and fall back to
   the last copy of the tool schemas, account, config, project list and integrations catalog so the field screen
   and the Integrations page open without signal. */
const SHELL_CACHE = 'tc-shell-v3';
const API_CACHE = 'tc-api-v1';
const SHELL = ['/', '/static/app.js', '/static/styles.css', '/manifest.webmanifest', '/icon.svg'];
const API_CACHEABLE = ['/api/tools', '/api/me', '/api/config', '/api/projects', '/api/integrations/catalog'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL_CACHE && k !== API_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('message', (event) => {
  if (event.data === 'clear-api-cache') event.waitUntil(caches.delete(API_CACHE));
});

function offlineJson() {
  return new Response(JSON.stringify({ detail: 'You are offline. This will work again when you reconnect.', offline: true }),
    { status: 503, headers: { 'Content-Type': 'application/json' } });
}

async function networkFirstApi(request) {
  const url = new URL(request.url);
  const cacheable = API_CACHEABLE.includes(url.pathname);
  try {
    const res = await fetch(request);
    if (cacheable && res.ok) {
      const copy = res.clone();
      caches.open(API_CACHE).then((c) => c.put(request, copy));
    }
    return res;
  } catch (err) {
    if (cacheable) {
      const hit = await caches.match(request, { cacheName: API_CACHE });
      if (hit) return hit;
    }
    return offlineJson();
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(SHELL_CACHE);
  const hit = await cache.match(request, { ignoreSearch: true });
  const fresh = fetch(request).then((res) => {
    if (res.ok) cache.put(request, res.clone());
    return res;
  }).catch(() => null);
  return hit || (await fresh) || new Response('Offline', { status: 503 });
}

async function navigation(request) {
  try {
    const res = await fetch(request);
    // Only the real app shell may become the offline copy of '/': caching any page navigated to (a 404, /health,
    // an error) would replace the app with that page offline.
    const path = new URL(request.url).pathname;
    if (res.ok && (path === '/' || path === '/index.html') && (res.headers.get('content-type') || '').includes('text/html')) {
      const cache = await caches.open(SHELL_CACHE);
      cache.put('/', res.clone());
    }
    return res;
  } catch (err) {
    return (await caches.match('/', { cacheName: SHELL_CACHE })) || new Response('Offline', { status: 503 });
  }
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname === '/mcp' || url.pathname.startsWith('/mcp/') || url.pathname === '/api/calendar.ics') return;
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(networkFirstApi(request));
  } else if (request.mode === 'navigate') {
    event.respondWith(navigation(request));
  } else if (SHELL.includes(url.pathname) || url.pathname.startsWith('/static/')) {
    event.respondWith(staleWhileRevalidate(request));
  }
});
