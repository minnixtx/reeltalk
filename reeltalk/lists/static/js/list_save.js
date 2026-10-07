/* The Save toggle on a list page (§2K increment 5, L2 / R140).

   Deliberately the same shape as the applaud toggle in likes.js — vanilla JS
   per R6, the csrf token from base.html's meta tag, a plain POST, and the
   JSON answer repaints the button in place with no reload.

   What the answer carries is the caller's own state and nothing else. A like
   returns a count as well because the tally beside the button has to stay in
   step with it; a save has no tally anywhere on the page, so there is no
   second fact here and nothing for the client to be permitted to guess at.

   Which URL gets posted to is read off the button rather than assembled
   here. The template hands over both `/save/` and `/unsave/` and this script
   only ever picks one of them based on the button's current state, so no URL
   shape lives in JavaScript that could drift from the one Django actually
   routes.

   Without JavaScript nothing breaks: the button is the only control this
   file drives, and it simply never answers. */
(function () {
  "use strict";

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  }

  function paint(btn, saved) {
    btn.setAttribute("aria-pressed", saved ? "true" : "false");
    btn.setAttribute("data-state", saved ? "saved" : "unsaved");
    btn.textContent = saved ? "Saved" : "Save";
    // The label is the whole affordance on a text button, so both the
    // visible word and the accessible name move together — a button that
    // reads "Saved" while still announcing "Save this list" is worse than
    // one that says nothing.
    var label = saved
      ? "Saved — click to remove this list"
      : "Save this list to your saved lists";
    btn.setAttribute("aria-label", label);
    btn.setAttribute("title", label);
  }

  function toggle(btn) {
    if (btn.disabled) {
      return;
    }
    var saved = btn.getAttribute("data-state") === "saved";
    var url = btn.getAttribute(saved ? "data-unsave-url" : "data-save-url");
    if (!url) {
      return;
    }
    // Locked while in flight, same as the applaud button: two clicks racing
    // would otherwise send a save and an unsave and leave the button on
    // whichever the server read last.
    btn.disabled = true;
    fetch(url, {
      method: "POST",
      headers: {
        "X-CSRFToken": csrfToken(),
        "Content-Type": "application/x-www-form-urlencoded",
      },
      credentials: "same-origin",
    })
      .then(function (resp) {
        return resp.json().then(function (data) {
          return { ok: resp.ok, data: data };
        });
      })
      .then(function (r) {
        btn.disabled = false;
        // Repaint only on a real answer. A refused or failed request leaves
        // the button showing the state the server last told it about, which
        // is the truth, rather than the state the click implied.
        if (r.ok && typeof r.data.saved === "boolean") {
          paint(btn, r.data.saved);
        }
      })
      .catch(function () {
        btn.disabled = false;
      });
  }

  function init() {
    // One delegated listener rather than one per button, for the same reason
    // likes.js does it that way: init() runs once, and anything added to the
    // page after it must still work.
    document.addEventListener("click", function (event) {
      var target = event.target;
      if (!target || typeof target.closest !== "function") {
        return;
      }
      var btn = target.closest(".list-save-btn");
      if (btn) {
        toggle(btn);
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
