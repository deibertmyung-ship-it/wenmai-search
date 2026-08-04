// Debug drawer: open/close, scrim, focus restore, Escape to dismiss.
(function () {
  var drawer = document.querySelector('[data-drawer]');
  var scrim = document.querySelector('[data-scrim]');
  if (!drawer) return;

  var opener = null;
  drawer.hidden = false;           // hidden until JS confirms it can drive it
  drawer.setAttribute('data-open', 'false');

  function open(trigger) {
    opener = trigger || null;
    drawer.setAttribute('data-open', 'true');
    if (scrim) scrim.setAttribute('data-open', 'true');
    var close = drawer.querySelector('[data-drawer-close]');
    if (close) close.focus();
  }

  function close() {
    drawer.setAttribute('data-open', 'false');
    if (scrim) scrim.setAttribute('data-open', 'false');
    if (opener) opener.focus();
  }

  document.querySelectorAll('[data-drawer-open]').forEach(function (button) {
    button.addEventListener('click', function () { open(button); });
  });
  document.querySelectorAll('[data-drawer-close]').forEach(function (button) {
    button.addEventListener('click', close);
  });
  if (scrim) scrim.addEventListener('click', close);
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && drawer.getAttribute('data-open') === 'true') close();
  });
})();
