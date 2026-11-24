/* Handing a stranger the address of the post you are reading (share arc S2).
   Deliberately small vanilla JS per R6 — no framework, no build step — and
   loaded from a file rather than inline because CSP script-src is 'self' with no
   'unsafe-inline': an inline handler would be blocked and the button would be
   inert, which is the same reason invite.js exists at all.

   The ladder is the owner's D7 answer, one rung above invite.js's: the operating
   system's own share sheet where the browser has one, the clipboard where it has
   none, and select-and-copy as the last resort. Three rules shape it.

   * Capability, not user agent. Desktop Chrome implements navigator.share on
     some platforms, so on those a press opens the OS sheet rather than copying.
     That is the ruling taken honestly, not a mobile check to be added.
   * A cancelled sheet is a no-op. navigator.share rejects when the reader closes
     the sheet without choosing anything, and falling through to the clipboard
     then would rewrite what they had copied because someone pressed Escape. So
     the copy rungs are reached only when navigator.share is ABSENT; a rejection
     is swallowed, AbortError included. (The prototype on the review page did
     `.catch(copy)` — that is how the rule was found.)
   * The clipboard API needs a secure context, and this instance serves plain HTTP
     on the LAN permanently, so there both share and clipboard are absent and the
     last rung is the normal path, not the rare one.

   The address comes off the button's own data-share-url, put there by the server
   through Status.share_url — which is empty whenever the post is not reachable
   offsite, so a page that renders this control at all has a real address in it.
   One listener on the document rather than one per button, for the reason
   test_feed_endless_scroll.py spells out for likes.js.

   What a press says out loud is one floating pill (owner ruling, 2026-11-23),
   not a line of text beside the icon. The inline version widened the control
   group inside a right-aligned bar, so the icon slid 59px sideways while the word
   was up and a second press landed on the row's own link instead of the button.
   A pill appended to `body` and positioned `fixed` belongs to no layout, so there
   is nothing for it to push. It is also the page's only live region for this
   action: one element with `role="status"` that every surface shares, created
   here rather than printed in four templates, so the announcement cannot drift
   between the feed row and the post page. */
(function () {
  "use strict";

  // How long each notice stands. "Press Ctrl+C" gets the longer window because the
  // reader has to read it and then press keys; "Link copied" only has to be seen.
  var RESET_MS = 1800;
  var HOLD_MS = 6000;

  // The last resort's selection lives in a textarea of our own, because there is
  // no visible field on these pages to select the way the invite box has one.
  var field = null;

  // Created once, at load, and left in the DOM between presses: a live region
  // that is added and removed in the same breath as its text is a region a
  // screen reader may never have seen at all. CSS keeps it invisible at rest.
  var toast = document.createElement("div");
  toast.className = "share-toast";
  toast.setAttribute("role", "status");
  document.body.appendChild(toast);
  var hideTimer = 0;

  function dropField() {
    if (field && field.parentNode) {
      field.parentNode.removeChild(field);
    }
    field = null;
  }

  function say(text, ms, then) {
    toast.textContent = text;
    toast.classList.add("is-up");
    window.clearTimeout(hideTimer);
    hideTimer = window.setTimeout(function () {
      toast.classList.remove("is-up");
      toast.textContent = "";
      if (then) {
        then();
      }
    }, ms);
  }

  function copyBySelecting(url) {
    dropField();
    field = document.createElement("textarea");
    field.className = "share-field";
    field.value = url;
    field.setAttribute("aria-hidden", "true");
    document.body.appendChild(field);
    // Focus and select are what make the next two branches both work: if the copy
    // lands, fine; if the browser refuses it, the text stays selected and the
    // keyboard shortcut still does the job.
    field.focus();
    field.select();
    var ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (err) {
      ok = false;
    }
    if (ok) {
      say("Link copied", RESET_MS, dropField);
    } else {
      say("Press Ctrl+C", HOLD_MS, dropField);
    }
  }

  function copy(url) {
    if (window.isSecureContext && navigator.clipboard) {
      navigator.clipboard.writeText(url).then(
        function () {
          say("Link copied", RESET_MS);
        },
        // This rejection is a failed write, not a cancelled sheet, so it does
        // fall through — unlike navigator.share's.
        function () {
          copyBySelecting(url);
        }
      );
      return;
    }
    copyBySelecting(url);
  }

  function share(url) {
    if (navigator.share) {
      // Nothing is announced here: the sheet is the feedback, and a closed sheet
      // means nothing happened. See the header for why the rejection stays shut.
      navigator.share({ url: url }).catch(function () {});
      return;
    }
    copy(url);
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest(".share-btn") : null;
    if (!btn) {
      return;
    }
    var url = btn.getAttribute("data-share-url");
    if (url) {
      share(url);
    }
  });
})();
