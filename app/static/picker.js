(function () {
  var input = document.getElementById('site-search');
  if (!input) return;
  var results = document.getElementById('site-results');
  var hidden = document.getElementById('site-id');
  var chosen = document.getElementById('site-chosen');
  var timer = null;

  input.addEventListener('input', function () {
    clearTimeout(timer);
    var q = input.value.trim();
    if (q.length < 2) { results.textContent = ''; return; }
    timer = setTimeout(function () {
      fetch('/api/sites?limit=15&q=' + encodeURIComponent(q), { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          results.textContent = '';
          data.sites.forEach(function (s) {
            var li = document.createElement('li');
            li.textContent = s.name + (s.types.length ? ' — ' + s.types.join(', ') : '');
            li.addEventListener('click', function () {
              hidden.value = s.id; chosen.textContent = s.name;
              results.textContent = ''; input.value = '';
            });
            results.appendChild(li);
          });
          if (!data.sites.length) results.innerHTML = '<li class="muted">Aucun résultat</li>';
        });
    }, 250);
  });
})();
