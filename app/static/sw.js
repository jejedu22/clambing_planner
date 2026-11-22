/* Service worker de Climbing Planner.
 * - Ressources statiques (CSS, JS, icônes, Leaflet) : mises en cache, rafraîchies en arrière-plan.
 * - Pages HTML et API : jamais mises en cache (données privées de club, session) ;
 *   hors connexion, on affiche la page /offline.
 * __VERSION__ est remplacé côté serveur : le cache est renouvelé à chaque déploiement.
 */
const VERSION = '__VERSION__';
const CACHE = 'climbing-' + VERSION;
const PRECACHE = [
  '/offline', '/static/style.css', '/static/map.js', '/static/picker.js', '/static/pwa.js',
  '/static/vendor/leaflet/leaflet.css', '/static/vendor/leaflet/leaflet.js',
  '/static/icons/icon-192.png',
];

self.addEventListener('install', function (event) {
  event.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(PRECACHE); })
    .then(function () { return self.skipWaiting(); }));
});

self.addEventListener('activate', function (event) {
  event.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k.startsWith('climbing-') && k !== CACHE; })
      .map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener('fetch', function (event) {
  var req = event.request;
  if (req.method !== 'GET') return;
  var url = new URL(req.url);
  if (url.origin !== location.origin) return;   // tuiles de carte, etc. : réseau uniquement

  if (url.pathname.startsWith('/static/')) {     // stale-while-revalidate
    event.respondWith(caches.open(CACHE).then(function (cache) {
      return cache.match(req).then(function (hit) {
        var fresh = fetch(req).then(function (res) {
          if (res.ok) cache.put(req, res.clone());
          return res;
        }).catch(function () { return hit; });
        return hit || fresh;
      });
    }));
    return;
  }

  if (req.mode === 'navigate') {                  // pages : réseau, sinon page hors-ligne
    event.respondWith(fetch(req).catch(function () { return caches.match('/offline'); }));
  }
});
