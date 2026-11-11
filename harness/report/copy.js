(function () {
  var btn = document.getElementById('rc-copy');
  if (!btn) { return; }

  function pageText() {
    // Hide the button so its own label never lands in the copied text.
    var previous = btn.style.display;
    btn.style.display = 'none';
    var root = document.querySelector('main') || document.body;
    var text = (root.innerText || root.textContent || '').replace(/\n{3,}/g, '\n\n').trim();
    btn.style.display = previous;
    return text;
  }

  function legacyCopy(text) {
    var field = document.createElement('textarea');
    field.value = text;
    field.setAttribute('readonly', '');
    field.style.position = 'fixed';
    field.style.top = '-1000px';
    document.body.appendChild(field);
    field.select();
    var copied = false;
    try { copied = document.execCommand('copy'); } catch (err) { copied = false; }
    document.body.removeChild(field);
    return copied;
  }

  function report(state) {
    btn.textContent = btn.getAttribute('data-' + state);
    btn.classList.toggle('is-done', state === 'done');
    btn.classList.toggle('is-fail', state === 'fail');
    window.setTimeout(function () {
      btn.textContent = btn.getAttribute('data-idle');
      btn.classList.remove('is-done', 'is-fail');
    }, 2400);
  }

  btn.addEventListener('click', function () {
    var text = pageText();
    // Some hosts block the async clipboard API inside the artifact iframe, so the
    // execCommand path stays as a real fallback rather than an assumed success.
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () { report('done'); },
        function () { report(legacyCopy(text) ? 'done' : 'fail'); }
      );
    } else {
      report(legacyCopy(text) ? 'done' : 'fail');
    }
  });
})();
