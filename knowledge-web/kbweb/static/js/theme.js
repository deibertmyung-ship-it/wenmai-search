// Theme toggle. The stored value must win over prefers-color-scheme in both
// directions, so we always write an explicit data-theme.
(function () {
  var KEY = 'kbweb-theme';
  var root = document.documentElement;

  function current() {
    var explicit = root.getAttribute('data-theme');
    if (explicit) return explicit;
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  document.querySelectorAll('[data-theme-toggle]').forEach(function (button) {
    button.addEventListener('click', function () {
      var next = current() === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem(KEY, next); } catch (e) { /* private mode */ }
      button.setAttribute('aria-pressed', String(next === 'dark'));
    });
  });
})();
