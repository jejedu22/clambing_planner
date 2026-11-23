(function () {
  var el = document.getElementById('location-map');
  if (!el || typeof L === 'undefined') return;
  L.Icon.Default.imagePath = '/static/vendor/leaflet/images/';
  var lat = document.getElementById('lat'), lon = document.getElementById('lon');
  var status = document.getElementById('location-status');
  var start = lat.value && lon.value ? [parseFloat(lat.value), parseFloat(lon.value)] : null;
  var map = L.map(el).setView(start || [46.6, 2.5], start ? 12 : 6);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 18, attribution: '© OpenStreetMap' }).addTo(map);
  var marker = start ? L.marker(start, { draggable: true }).addTo(map) : null;

  function place(latlng, zoom) {
    lat.value = latlng.lat.toFixed(6); lon.value = latlng.lng.toFixed(6);
    if (marker) { marker.setLatLng(latlng); } else {
      marker = L.marker(latlng, { draggable: true }).addTo(map);
      marker.on('dragend', function () { place(marker.getLatLng()); });
    }
    if (zoom) map.setView(latlng, zoom);
    status.textContent = 'Point choisi : ' + lat.value + ', ' + lon.value + '. Pensez à enregistrer.';
  }
  if (marker) marker.on('dragend', function () { place(marker.getLatLng()); });
  map.on('click', function (e) { place(e.latlng); });

  var geo = document.getElementById('use-position');
  if (geo) {
    if (!('geolocation' in navigator)) { geo.hidden = true; return; }
    geo.addEventListener('click', function () {
      status.textContent = 'Localisation en cours…';
      navigator.geolocation.getCurrentPosition(
        function (p) { place(L.latLng(p.coords.latitude, p.coords.longitude), 13); },
        function () { status.textContent = 'Position indisponible : cliquez directement sur la carte.'; },
        { enableHighAccuracy: false, timeout: 10000 });
    });
  }
})();
