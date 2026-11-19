/* The two in-place toggles a feed row and a post page carry (applaud and
   replay). They are one file because they are one mechanism: POST to the
   control's own data-url, csrf from the base.html meta tag, and repaint from
   the JSON answer with no reload. The answer carries both the caller's own
   state and the new total, so one round trip keeps the button and the tally
   consistent instead of letting the client guess one of them.

   The applaud half is feed interactions increment 3 (R83 decision 4), restyled
   to the bare icon button on the owner's pass of 2026-10-05 and re-drawn as a
   pair of hands on the pass of 2026-11-15. The wire stays "like" — route, model
   and ActivityPub verb are unchanged; only the visible control changed. The
   replay half arrived with replay arc P2 and changed nothing about the
   mechanism: it only added a second entry to the table below, which is the
   whole reason the listener is written the way it is.

   Deliberately small vanilla JS per R6 — no framework, no build step. Same shape
   as the one-click watchlist control in search.js. */
(function () {
  "use strict";

  // What each control's answer looks like: the key the state arrives under, the
  // count to repaint, and the two labels. Keyed by the class the click matched,
  // so a second toggle is a row in this table and not a second listener — see
  // init() for why a second listener is the thing to avoid.
  var CONTROLS = {
    "applaud-btn": {
      stateKey: "liked",
      countClass: ".applaud-count",
      mine: "Applauded — click to remove",
      idle: "Applaud"
    },
    "replay-btn": {
      stateKey: "replayed",
      countClass: ".replay-count",
      mine: "Replayed — click to remove",
      idle: "Replay"
    }
  };

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  }

  function controlFor(btn) {
    return btn.classList.contains("replay-btn")
      ? CONTROLS["replay-btn"]
      : CONTROLS["applaud-btn"];
  }

  function paint(btn, control, on, count) {
    // The count is a SIBLING of the button rather than a child of it, so that
    // clicking the number cannot toggle anything. Find it through the wrapper.
    var holder = btn.parentNode;
    var shown = holder ? holder.querySelector(control.countClass) : null;
    if (shown && typeof count === "number") {
      shown.textContent = count;
    }
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    // The resting icon answers ONE question — did *I* do this — so the state is
    // the caller's own and nothing else. Whether anyone else did is the
    // counter's job (owner pass, 2026-11-15, which took the old "someone else
    // applauded" state off the ink). Both halves still come back from the
    // server, so neither can drift out of step with the other the way a
    // client-side increment would allow.
    btn.setAttribute("data-state", on ? "mine" : "idle");
    // The control carries no visible text, so the tooltip and the accessible
    // name are the only thing that says what it is and what it will do.
    var label = on ? control.mine : control.idle;
    btn.setAttribute("aria-label", label);
    btn.setAttribute("title", label);
  }

  function toggle(btn, control) {
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
        if (r.ok && typeof r.data[control.stateKey] === "boolean") {
          paint(btn, control, r.data[control.stateKey], r.data.count);
        }
      })
      .catch(function () {
        btn.disabled = false;
      });
  }

  function init() {
    /* ONE delegated listener on the document, not one per button and not one
       per control type. The endless scroll (§2I increment 3) appends rows long
       after this runs, and init() fires exactly once at DOMContentLoaded — so a
       listener bound per button here would leave every button on an appended
       row dead. Delegation is the fix and it is cheaper besides: one listener
       however many rows the feed grows to, and however many kinds of button
       those rows carry. */
    document.addEventListener("click", function (event) {
      var target = event.target;
      if (!target || typeof target.closest !== "function") {
        return;
      }
      var btn = target.closest(".applaud-btn, .replay-btn");
      if (btn) {
        toggle(btn, controlFor(btn));
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
