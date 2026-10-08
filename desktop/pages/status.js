'use strict';
// Splash / "Stopping safely…" page. The main process passes the texts in the query
// string (loadFile(..., {query})); they are inserted as text, never as HTML.
(function () {
  const params = new URLSearchParams(window.location.search);
  const title = params.get('title');
  const detail = params.get('detail');
  if (title) {
    document.getElementById('title').textContent = title;
    document.title = title;
  }
  document.getElementById('detail').textContent = detail || '';
  if (params.get('busy') === '0') document.getElementById('spinner').hidden = true;
})();
