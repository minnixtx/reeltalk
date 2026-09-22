/* Reply composer (feed interactions increment 4, R83 decision 2). Same
   shape as likes.js per R6 — csrf from the base.html meta tag, the control
   locked while in flight, and a repaint only on a real answer. What the
   answer carries is the difference: not a boolean and a tally but the new
   reply row already rendered by the same Django partial the page's thread
   loop uses. The client inserts server-rendered markup rather than
   rebuilding it here, so the appended row cannot drift from the template.
   A failed request leaves the textarea exactly as it was — text the server
   did not take is not thrown away, and the thread is not touched. */
(function () {
  "use strict";

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  }

  function paintThread(count) {
    if (typeof count !== "number") {
      return;
    }
    var heading = document.getElementById("reply-count");
    if (heading) {
      heading.textContent = "Replies (" + count + ")";
    }
    // The empty-state line only belongs to a thread with nothing in it.
    if (count > 0) {
      var empty = document.querySelector(".status-replies .no-replies");
      if (empty) {
        empty.remove();
      }
    }
  }

  function init() {
    document.querySelectorAll("form.reply-form").forEach(function (form) {
      var btn = form.querySelector('button[type="submit"]');
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        if (btn && btn.disabled) {
          return;
        }
        // Locked while in flight: a second submit would add a second reply
        // before the first answer came back.
        if (btn) {
          btn.disabled = true;
        }
        fetch(form.getAttribute("action"), {
          method: "POST",
          headers: {
            "X-CSRFToken": csrfToken(),
            "Content-Type": "application/x-www-form-urlencoded",
          },
          credentials: "same-origin",
          body: new URLSearchParams(new FormData(form)).toString(),
        })
          .then(function (resp) {
            return resp.json().then(function (data) {
              return { ok: resp.ok, data: data };
            });
          })
          .then(function (r) {
            if (btn) {
              btn.disabled = false;
            }
            if (!r.ok || typeof r.data.html !== "string") {
              return;
            }
            var list = document.querySelector(".status-replies .thread-list");
            if (!list) {
              return;
            }
            list.insertAdjacentHTML("beforeend", r.data.html);
            paintThread(r.data.count);
            form.reset();
          })
          .catch(function () {
            if (btn) {
              btn.disabled = false;
            }
          });
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
