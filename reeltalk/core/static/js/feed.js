/* Endless scroll for the home feed (§2I increment 3, R132 decisions 4, 5 and 6).
   Deliberately small vanilla JS per R6 — the same shape as likes.js: read the
   state out of the rendered DOM, make a plain fetch, splice the server's HTML
   into the page. No framework and no client-side templating, because the rows
   are rendered by the same Django partial the full page uses and a JS-built copy
   of a row is exactly the drift this increment exists to prevent. */
(function () {
  "use strict";

  /* The DOM ceiling (R132 decision 4). Rows are not recycled off-screen — there is
     no virtualisation here — so past this many the observer disconnects and the
     "Older" link becomes the only way forward. The property that matters is
     that the feed never silently stops growing: there is always a visible way to
     continue, which is why hitting the ceiling un-hides the link rather than
     just stopping. */
  var CEILING = 300;

  function appendRows(list, html) {
    /* Parsed through <template>, whose content is parsed detached and so accepts
       a bare run of <li> elements that would be invalid anywhere else in the
       document. The nodes are then moved, not cloned, into the list. */
    var tpl = document.createElement("template");
    tpl.innerHTML = html;
    var nodes = Array.prototype.slice.call(tpl.content.children);
    for (var i = 0; i < nodes.length; i++) {
      list.appendChild(nodes[i]);
    }
    return nodes;
  }

  function start() {
    var list = document.getElementById("feed-list");
    var sentinel = document.getElementById("feed-sentinel");
    var more = document.getElementById("feed-more");
    var older = document.getElementById("feed-older");
    if (!list || !sentinel || !older) {
      return;
    }

    var next = sentinel.getAttribute("data-next") || "";
    if (!next) {
      /* One page of feed: nothing to load, so nothing to observe and nothing to
         hide. The "Older" link is absent here for the same reason. */
      return;
    }

    if (!("IntersectionObserver" in window)) {
      /* Degraded, not broken: the server-rendered "Older" link is still on the
         page and still works. This check has to come BEFORE the link is hidden,
         or a browser without the observer would be left with neither. */
      return;
    }

    /* JS takes over the trigger; the link stays in the DOM as the fallback
       (decision 5). The whole paragraph is hidden rather than the anchor alone,
       so an empty <p> does not leave a gap where the link was. Its href is moved
       forward on every page so that whenever it is revealed it points at the
       page that actually comes next. */
    more.hidden = true;

    var loading = false;
    var observer = null;

    function stop(revealOlder) {
      if (observer) {
        observer.disconnect();
      }
      if (revealOlder) {
        older.href = "/?c=" + encodeURIComponent(next);
        more.hidden = false;
      }
    }

    function load() {
      if (loading || !next) {
        return;
      }
      loading = true;
      fetch(
        "/feed/page/?c=" + encodeURIComponent(next),
        { credentials: "same-origin" }
      )
        .then(function (resp) {
          return resp.ok ? resp.text() : "";
        })
        .then(function (html) {
          loading = false;
          var added = html ? appendRows(list, html) : [];
          if (!added.length) {
            /* Nothing came back. Stop rather than hammer the route for the same
               empty answer forever — and leave the link visible, because the
               reader still has somewhere to go. */
            stop(true);
            return;
          }
          var last = added[added.length - 1];
          next = last.getAttribute("data-cursor") || "";
          if (last.classList.contains("feed-end")) {
            stop(false);
            return;
          }
          if (list.querySelectorAll("li.review").length >= CEILING) {
            stop(true);
          }
        })
        .catch(function () {
          loading = false;
          /* A failed request leaves the page as it was, with the link as the
             way forward. Same rule as a failed Like: never repaint into a guess. */
          stop(true);
        });
    }

    observer = new IntersectionObserver(function (entries) {
      for (var i = 0; i < entries.length; i++) {
        if (entries[i].isIntersecting) {
          load();
        }
      }
    });
    observer.observe(sentinel);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
