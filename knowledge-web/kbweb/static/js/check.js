// Progress over SSE, plus opening the matching passage card when a highlight
// is clicked. Both are enhancements: the page already renders its state
// server-side and the cards are <details>, which open without any script.
(function () {
  var panel = document.querySelector('[data-check-progress]');

  if (panel) {
    var STAGES = {
      queued: '排队中',
      started: '开始检测',
      chunking: '切分文本',
      retrieving: '检索候选',
      aligning: '对齐比对',
      persisting: '写入结果'
    };
    var stageEl = panel.querySelector('[data-progress-stage]');
    var barEl = panel.querySelector('[data-progress-bar]');
    var source = new EventSource(panel.dataset.eventsUrl);
    var fallbackTimer = null;

    // SSE is an enhancement, not the only way out of the progress page. A
    // healthy stream emits keepalives every ~2s; if neither data nor keepalive
    // arrives for 15s, reload and let the server render the current state.
    function armFallback() {
      if (fallbackTimer) window.clearTimeout(fallbackTimer);
      fallbackTimer = window.setTimeout(function () {
        source.close();
        window.location.reload();
      }, 15000);
    }

    // Start the deadline immediately as well: a connection can hang before
    // firing either open or error.
    armFallback();
    source.onopen = armFallback;
    source.onerror = function () {
      // Keep native EventSource reconnection (and Last-Event-ID) alive, but do
      // not let repeated errors postpone the fallback forever.
      if (!fallbackTimer) armFallback();
    };
    source.addEventListener('keepalive', armFallback);

    Object.keys(STAGES).forEach(function (stage) {
      source.addEventListener(stage, function (event) {
        armFallback();
        stageEl.textContent = STAGES[stage];
        try {
          var payload = JSON.parse(event.data);
          if (typeof payload.progress === 'number') {
            barEl.style.width = Math.round(payload.progress * 100) + '%';
          }
        } catch (e) { /* a malformed frame must not stop the stream */ }
      });
    });

    ['completed', 'completed_partial', 'failed', 'cancelled'].forEach(function (stage) {
      source.addEventListener(stage, function () {
        if (fallbackTimer) window.clearTimeout(fallbackTimer);
        source.close();
        // Let the server render the report; there is one renderer, not two.
        window.location.reload();
      });
    });
  }

  // Clicking a marker in the submission opens its card. The href already
  // jumps to the source, so this only adds the expansion.
  document.querySelectorAll('.hit__marker').forEach(function (marker) {
    marker.addEventListener('click', function () {
      var target = document.querySelector(marker.getAttribute('href'));
      if (!target) return;
      target.querySelectorAll('details').forEach(function (card) { card.open = true; });
    });
  });
})();
