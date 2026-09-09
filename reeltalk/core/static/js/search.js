/* ReelTalk search behaviour (M2 increment 4). Deliberately small vanilla JS
   per R6 — no framework, no build step. Two jobs: the header
   search-as-you-type dropdown and the one-click watchlist buttons on the
   search results page. */
(function () {
  "use strict";

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  }

  // --- Header suggest dropdown ---------------------------------------------

  function initSuggest() {
    var form = document.querySelector(".global-search");
    if (!form) {
      return;
    }
    var input = form.querySelector("input[type=search]");
    var list = document.getElementById("search-suggest");
    var timer = null;

    function hide() {
      list.hidden = true;
      list.innerHTML = "";
    }

    function render(results) {
      if (!results.length) {
        hide();
        return;
      }
      list.innerHTML = "";
      results.forEach(function (row) {
        var li = document.createElement("li");
        var a = document.createElement("a");
        a.href = row.url;
        a.textContent = row.title + (row.year ? " (" + row.year + ")" : "");
        li.appendChild(a);
        list.appendChild(li);
      });
      list.hidden = false;
    }

    input.addEventListener("input", function () {
      var q = input.value.trim();
      if (timer) {
        clearTimeout(timer);
      }
      if (q.length < 2) {
        hide();
        return;
      }
      // Debounce so a word costs a few requests, not one per keystroke.
      timer = setTimeout(function () {
        fetch("/search/suggest/?q=" + encodeURIComponent(q))
          .then(function (resp) {
            return resp.json();
          })
          .then(function (data) {
            render(data.results || []);
          })
          .catch(hide);
      }, 200);
    });

    input.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        hide();
      }
    });

    document.addEventListener("click", function (e) {
      if (!form.contains(e.target)) {
        hide();
      }
    });
  }

  // --- One-click watchlist on search rows -----------------------------------

  function initWatchlistButtons() {
    document.querySelectorAll(".watchlist-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var url = btn.getAttribute("data-url");
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
            var status = r.data.status;
            if (status === "added" || status === "already") {
              btn.textContent = "On watchlist";
              btn.disabled = true;
            } else if (status === "watched") {
              btn.textContent = "Already watched";
              btn.disabled = true;
            } else {
              btn.textContent = r.data.error || "Failed — try again";
            }
          })
          .catch(function () {
            btn.textContent = "Failed — try again";
          });
      });
    });
  }

  function init() {
    initSuggest();
    initWatchlistButtons();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
