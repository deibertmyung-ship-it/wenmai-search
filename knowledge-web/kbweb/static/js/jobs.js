// Poll the jobs table only while something can still change, and back off so a
// forgotten tab does not hammer the backend.
(function () {
  var table = document.querySelector('[data-jobs-table]');
  if (!table || table.dataset.live !== 'true') return;

  var url = table.dataset.pollUrl;
  var TERMINAL = { completed: 1, failed: 1, cancelled: 1 };
  var delay = 2000;
  var MAX_DELAY = 30000;

  function tick() {
    if (document.hidden) { schedule(); return; }

    fetch(url, { headers: { Accept: 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
      .then(function (data) {
        var live = (data.jobs || []).some(function (job) { return !TERMINAL[job.state]; });
        if (!live) {
          // Everything settled: one reload to render the final table server-side.
          window.location.reload();
          return;
        }
        delay = Math.min(Math.round(delay * 1.4), MAX_DELAY);
        schedule();
      })
      .catch(function () {
        delay = Math.min(Math.round(delay * 2), MAX_DELAY);
        schedule();
      });
  }

  function schedule() { window.setTimeout(tick, delay); }
  schedule();
})();
