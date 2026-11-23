(function () {
  document.querySelectorAll('[data-copy]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var input = document.getElementById(btn.getAttribute('data-copy'));
      if (!input) return;
      var done = function () { var t = btn.textContent; btn.textContent = 'Copié !'; setTimeout(function () { btn.textContent = t; }, 1800); };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(input.value).then(done, function () { input.select(); });
      } else { input.classList.remove('sr-only'); input.select(); document.execCommand('copy'); done(); }
    });
  });
})();
