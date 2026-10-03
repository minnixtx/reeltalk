/* Like toggle (feed interactions increment 3, R83 decision 4). Deliberately
   small vanilla JS per R6 — no framework, no build step. Same shape as the
   one-click watchlist control in search.js: the csrf token comes from the
   base.html meta tag, the request is a plain POST, and the JSON answer
   updates the button in place with no reload. The answer carries both the
   caller's own state and the new total, so one round trip keeps the button
   and the tally consistent instead of letting the client guess one of them. */
(function () {
  "use strict";

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  }

  function paint(btn, liked, count) {
    var label = btn.querySelector(".like-label");
    var shown = btn.querySelector(".like-count");
    if (label) {
      label.textContent = liked ? "Liked" : "Like";
    }
    if (shown && typeof count === "number") {
      shown.textContent = count;
    }
    btn.setAttribute("aria-pressed", liked ? "true" : "false");
  }

  function toggle(btn) {
    if (btn.disabled) {
      return;
    }
    // Locked while in flight: two clicks racing would otherwise send two
    // toggles and leave the button on whichever the server read last.
    btn.disabled = true;
    fetch(btn.getAttribute("data-url"), {
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
        // Repaint only on a real answer. A failed request leaves the
        // button showing the state it was in rather than a guess.
        if (r.ok && typeof r.data.liked === "boolean") {
          paint(btn, r.data.liked, r.data.count);
        }
      })
      .catch(function () {
        btn.disabled = false;
      });
  }

  function init() {
    /* ONE delegated listener on the document, not one per .like-btn.
       The endless scroll (§2I increment 3) appends rows long after this runs,
       and init() fires exactly once at DOMContentLoaded — so a listener bound
       per button here would leave every Like button on an appended row dead.
       Delegation is the fix and it is cheaper besides: one listener however
       many rows the feed grows to. */
    document.addEventListener("click", function (event) {
      var target = event.target;
      if (!target || typeof target.closest !== "function") {
        return;
      }
      var btn = target.closest(".like-btn");
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
