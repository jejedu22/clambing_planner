(function () {
  var el = document.getElementById('map');
  if (!el || typeof L === 'undefined') return;
  L.Icon.Default.imagePath = '/static/vendor/leaflet/images/';
  var tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18, attribution: '© OpenStreetMap'
  });

  function esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

  if (el.dataset.lat) { // fiche d'un site
    var pos = [parseFloat(el.dataset.lat), parseFloat(el.dataset.lon)];
    var m = L.map(el).setView(pos, 14);
    tiles.addTo(m); L.marker(pos).addTo(m);
    return;
  }

  // carte des sites : cadrée sur la localisation du club et ses favoris, sinon la France
  var view = {};
  try { view = JSON.parse(el.dataset.view || '{}') || {}; } catch (e) { view = {}; }
  var map = L.map(el);
  var fit = (view.fit || []).filter(function (p) { return isFinite(p[0]) && isFinite(p[1]); });
  if (fit.length > 1) {
    map.fitBounds(L.latLngBounds(fit), { padding: [40, 40], maxZoom: 11 });
  } else if (fit.length === 1) {
    map.setView(fit[0], view.home ? 10 : 11);
  } else {
    map.setView([46.6, 2.5], 6);
  }
  tiles.addTo(map);
  if (view.home) {
    var homeIcon = L.divIcon({ className: 'home-icon', iconSize: [30, 30], iconAnchor: [15, 15],
      html: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 10 12 3l9 7v10a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1Z"/></svg>' });
    L.marker([view.home.lat, view.home.lon], { icon: homeIcon, title: view.home.label, zIndexOffset: 1000, keyboard: true })
      .addTo(map).bindPopup(esc(view.home.label));
  }
  var layer = L.layerGroup().addTo(map);
  var note = document.getElementById('map-note');
  var timer = null;


  function load() {
    var b = map.getBounds();
    var qs = new URLSearchParams({
      bbox: [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()].join(','),
      kind: el.dataset.kind || '', outdoor: el.dataset.outdoor || '0', q: el.dataset.q || '', fav: el.dataset.fav || '0', limit: 400
    });
    fetch('/api/sites?' + qs, { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        layer.clearLayers();
        data.sites.forEach(function (s) {
          var opts = s.fav ? { icon: L.divIcon({ className: 'star-icon', html: '⭐', iconSize: [22, 22], iconAnchor: [11, 11] }) } : {};
          L.marker([s.lat, s.lon], opts).addTo(layer).bindPopup(
            '<a href="/sites/' + s.id + '">' + (s.fav ? '⭐ ' : '') + esc(s.name) + '</a><br>' + esc(s.types.join(', ')));
        });
        note.textContent = data.truncated
          ? 'Trop de sites dans cette zone : zoomez pour tous les voir.'
          : data.sites.length + ' site(s) dans la zone affichée.';
      })
      .catch(function () { note.textContent = 'Chargement des sites impossible.'; });
  }
  map.on('moveend', function () { clearTimeout(timer); timer = setTimeout(load, 250); });
  load();
})();
