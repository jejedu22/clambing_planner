(function () {
  var el = document.getElementById('map');
  if (!el || typeof L === 'undefined') return;
  L.Icon.Default.imagePath = '/static/vendor/leaflet/images/';
  var tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18, attribution: '© OpenStreetMap'
  });

  if (el.dataset.lat) { // fiche d'un site
    var pos = [parseFloat(el.dataset.lat), parseFloat(el.dataset.lon)];
    var m = L.map(el).setView(pos, 14);
    tiles.addTo(m); L.marker(pos).addTo(m);
    return;
  }

  // carte de la France, sites chargés selon la zone visible
  var map = L.map(el).setView([46.6, 2.5], 6);
  tiles.addTo(map);
  var layer = L.layerGroup().addTo(map);
  var note = document.getElementById('map-note');
  var timer = null;

  function esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

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
