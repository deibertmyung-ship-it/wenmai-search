// Submit the library title filter as soon as the reader chooses a book.
// The conventional submit button remains as a load-failure fallback and is
// hidden only after this enhancement initializes successfully.
(function () {
  var select = document.querySelector('[data-library-title-filter]');
  if (!select || !select.form) return;
  select.form.setAttribute('data-auto-submit-ready', '');

  select.addEventListener('change', function () {
    select.form.requestSubmit();
  });
})();
