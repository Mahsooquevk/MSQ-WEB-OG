const CACHE_NAME = 'pfbank-v1';
const ASSETS = [
  '/',
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png'
];

// Install - cache basic assets
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(ASSETS))
  );
  self.skipWaiting();
});

// Activate - clean old caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch - network first, fallback to cache
self.addEventListener('fetch', event => {
  // Skip non-GET and chrome-extension requests
  if (event.request.method !== 'GET') return;
  if (event.request.url.startsWith('chrome-extension')) return;

  event.respondWith(
    fetch(event.request)
      .then(response => {
        // Cache successful responses
        const clone = response.clone();
        caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        return response;
      })
      .catch(() => {
        // Offline fallback
        return caches.match(event.request).then(cached => {
          if (cached) return cached;
          // If page not cached, show offline message
          if (event.request.destination === 'document') {
            return new Response(`
              <!DOCTYPE html>
              <html>
              <head>
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <style>
                  body { background: #0f2027; color: white; display: flex; align-items: center;
                         justify-content: center; height: 100vh; margin: 0; font-family: Arial; text-align: center; }
                  .box { padding: 30px; }
                  .icon { font-size: 60px; }
                  h2 { color: #00c6ff; margin: 15px 0; }
                  p { opacity: 0.8; }
                  button { margin-top: 20px; padding: 12px 30px; background: linear-gradient(90deg,#00c6ff,#0072ff);
                           border: none; border-radius: 8px; color: white; font-size: 16px; cursor: pointer; }
                </style>
              </head>
              <body>
                <div class="box">
                  <div class="icon">📡</div>
                  <h2>No Internet Connection</h2>
                  <p>Please check your connection and try again.</p>
                  <button onclick="location.reload()">Retry</button>
                </div>
              </body>
              </html>
            `, { headers: { 'Content-Type': 'text/html' } });
          }
        });
      })
  );
});
