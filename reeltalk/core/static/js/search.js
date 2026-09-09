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
    // R32: the rendered row links and the keyboard-active one (-1 = none).
    var items = [];
    var active = -1;

    function hide() {
      list.hidden = true;
      list.innerHTML = "";
      items = [];
      active = -1;
    }

    function setActive(index) {
      if (!items.length) {
        return;
      }
      items.forEach(function (a, i) {
        a.classList.toggle("active", i === index);
      });
      active = index;
      items[index].scrollIntoView({ block: "nearest" });
    }

    function render(results) {
      if (!results.length) {
        hide();
        return;
      }
      list.innerHTML = "";
      items = [];
      active = -1;
      results.forEach(function (row) {
        var li = document.createElement("li");
        var a = document.createElement("a");
        a.href = row.url;
        // R32: hovering moves the keyboard highlight to the same row.
        a.addEventListener("mouseenter", function () {
          setActive(items.indexOf(a));
        });
        // R31: a small poster beside the title so a film can be recognized by
        // its artwork; a placeholder keeps the row shape when there is none.
        if (row.poster_url) {
          var img = document.createElement("img");
          img.className = "suggest-thumb";
          img.src = row.poster_url;
          img.alt = "";
          a.appendChild(img);
        } else {
          var ph = document.createElement("span");
          ph.className = "suggest-thumb suggest-thumb-placeholder";
          a.appendChild(ph);
        }
        var label = document.createElement("span");
        label.textContent = row.title + (row.year ? " (" + row.year + ")" : "");
        a.appendChild(label);
        li.appendChild(a);
        list.appendChild(li);
        items.push(a);
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

    // R32: standard combobox keys. With no row active, Enter keeps its
    // default — submitting the search form for the typed query.
    input.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        hide();
        return;
      }
      if (!items.length) {
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActive((active + 1) % items.length);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActive(
          active < 0 ? items.length - 1 : (active - 1 + items.length) % items.length
        );
      } else if (e.key === "Enter" && active >= 0) {
        e.preventDefault();
        window.location.href = items[active].href;
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
