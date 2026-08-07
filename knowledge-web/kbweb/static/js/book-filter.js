// Narrow the book dropdown to the selected directory.
//
// The server renders every book grouped by directory, so this is presentation
// only - with JS off the full list stays selectable and the backend still
// intersects the two filters. Hiding <optgroup> is not reliable across browsers,
// so groups are detached and re-inserted in their original order instead.
(function () {
  var sources = document.getElementById('source_id');
  var books = document.getElementById('document_id');
  if (!sources || !books) return;

  var groups = Array.prototype.slice.call(books.querySelectorAll('optgroup'));
  if (!groups.length) return;

  var anchor = document.createComment('book-groups');
  books.appendChild(anchor);

  function apply() {
    var wanted = sources.value;
    var selected = books.value;
    var stillVisible = false;

    groups.forEach(function (group) {
      var show = !wanted || group.dataset.source === wanted;
      if (show) {
        books.insertBefore(group, anchor);
        if (group.querySelector('option[value="' + selected + '"]')) stillVisible = true;
      } else if (group.parentNode === books) {
        books.removeChild(group);
      }
    });

    // A book from a directory the user just switched away from would otherwise
    // stay selected but invisible, and get submitted anyway.
    if (selected && !stillVisible) books.value = '';
  }

  sources.addEventListener('change', apply);
  apply();
})();
