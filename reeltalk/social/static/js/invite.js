/* Copy-the-invite-link (R82). Deliberately small vanilla JS per R6 — no
   framework, no build step. Loaded from a file rather than inline because
   CSP_SCRIPT_SRC is 'self' with no 'unsafe-inline': an inline handler would
   be blocked and the button would do nothing at all. */
(function () {
  "use strict";

  var RESET_MS = 1800;

  function flash(btn, text) {
    if (!btn.dataset.label) {
      btn.dataset.label = btn.textContent;
    }
    btn.textContent = text;
    if (btn.dataset.timer) {
      window.clearTimeout(Number(btn.dataset.timer));
    }
    btn.dataset.timer = String(
      window.setTimeout(function () {
        btn.textContent = btn.dataset.label;
      }, RESET_MS)
    );
  }

  // The clipboard API only exists in a secure context, and this instance
  // serves plain HTTP on the LAN permanently — so on that origin this is
  // not a courtesy for old browsers, it is the only path. If it fails too,
  // the text is left selected so the keyboard shortcut still works.
  function copyBySelecting(input) {
    var wasReadonly = input.hasAttribute("readonly");
    if (wasReadonly) {
      // Some mobile browsers refuse to select a readonly field's contents.
      input.removeAttribute("readonly");
    }
    input.focus();
    input.select();
    try {
      input.setSelectionRange(0, input.value.length);
    } catch (err) {
      /* already selected */
    }
    var ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (err) {
      ok = false;
    }
    if (wasReadonly) {
      input.setAttribute("readonly", "readonly");
    }
    return ok;
  }

  function copy(input, btn) {
    if (window.isSecureContext && navigator.clipboard) {
      navigator.clipboard
        .writeText(input.value)
        .then(function () {
          flash(btn, "Copied!");
        })
        .catch(function () {
          flash(btn, copyBySelecting(input) ? "Copied!" : "Press Ctrl+C");
        });
      return;
    }
    flash(btn, copyBySelecting(input) ? "Copied!" : "Press Ctrl+C");
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("[data-copy-target]") : null;
    if (!btn) {
      return;
    }
    var input = document.getElementById(btn.getAttribute("data-copy-target"));
    if (input && input.value) {
      copy(input, btn);
    }
  });
})();
