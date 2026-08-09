// Reader: append the next batch of chunks in place. The server-rendered
// "next page" link stays as the no-JS fallback and is only removed once this
// script has taken over.
(function () {
  var reader = document.querySelector('[data-reader]');
  if (!reader) return;

  var container = reader.querySelector('[data-chunks]');
  var moreBox = reader.querySelector('[data-more]');
  if (!container || !moreBox) return;

  var nextFrom = parseInt(reader.dataset.nextFrom, 10) || 0;
  var hasMore = reader.dataset.hasMore === 'true';
  var pageSize = parseInt(reader.dataset.pageSize, 10) || 12;
  var url = reader.dataset.chunksUrl;
  var loading = false;
  // Mirrors the server-side running-head rule: only print a breadcrumb where the
  // section changes, so appended chunks match what was rendered above them.
  var lastHeading = (function () {
    var headings = container.querySelectorAll('.chunk__heading');
    return headings.length ? headings[headings.length - 1].textContent : null;
  })();

  var button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn btn--ghost';
  button.textContent = '续读下一段';
  moreBox.replaceChildren(button);

  function pageUrl(from) {
    return url + (url.indexOf('?') >= 0 ? '&' : '?') + 'from=' + from + '&limit=' + pageSize;
  }

  function render(chunk) {
    var section = document.createElement('section');
    section.className = 'chunk';
    section.id = 'c' + chunk.ordinal;

    var ordinal = document.createElement('span');
    ordinal.className = 'chunk__ordinal';
    ordinal.textContent = chunk.ordinal;

    var body = document.createElement('div');
    body.className = 'chunk__body';

    if (chunk.heading_path && chunk.heading_path.length) {
      var crumb = chunk.heading_path.join(' \u203a ');
      if (crumb !== lastHeading) {
        var heading = document.createElement('p');
        heading.className = 'chunk__heading';
        heading.textContent = crumb;
        body.appendChild(heading);
      }
      lastHeading = crumb;
    }

    var text = document.createElement('p');
    text.className = 'chunk__text';
    text.textContent = chunk.text;   // textContent, never innerHTML
    body.appendChild(text);

    section.append(ordinal, body);
    return section;
  }

  function load() {
    if (loading || !hasMore) return;
    loading = true;
    button.disabled = true;
    button.textContent = '读取中…';

    fetch(pageUrl(nextFrom), {
      headers: { Accept: 'application/json' }
    })
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
      })
      .then(function (data) {
        var fragment = document.createDocumentFragment();
        (data.chunks || []).forEach(function (chunk) { fragment.appendChild(render(chunk)); });
        container.appendChild(fragment);
        nextFrom = data.next_from;
        hasMore = data.has_more;
        if (!hasMore) {
          moreBox.replaceChildren(Object.assign(document.createElement('p'), {
            className: 'chunk__ordinal', textContent: '— 全文终 —'
          }));
        }
      })
      .catch(function () {
        // Fall back to full-page navigation rather than stranding the reader.
        var link = document.createElement('a');
        link.className = 'btn btn--ghost';
        link.href = pageUrl(nextFrom);
        link.textContent = '续读下一段';
        moreBox.replaceChildren(link);
      })
      .finally(function () {
        loading = false;
        button.disabled = false;
        button.textContent = '续读下一段';
      });
  }

  button.addEventListener('click', load);

  // Auto-load when the button scrolls into view, but only for users who
  // have not asked for reduced motion.
  if ('IntersectionObserver' in window &&
      !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    new IntersectionObserver(function (entries) {
      if (entries.some(function (e) { return e.isIntersecting; })) load();
    }, { rootMargin: '400px' }).observe(moreBox);
  }
})();
