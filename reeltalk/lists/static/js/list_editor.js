/* The list editor's add-film typeahead (§2K increment 3).

   Vanilla JS per R6 — no framework, no build step — and deliberately the same
   shape as the header search's suggest dropdown in search.js: same debounce,
   same two-character floor, same keyboard model, same poster-plus-title row.
   One thing differs, and it is the whole reason this file exists rather than
   the header's script being reused.

   Up there, picking a row navigates. Down here, picking a row ADDS. So the
   click fills the editor's hidden add form and submits it, which keeps the
   write an ordinary form post: the ranked list redraws from the database on
   the next page load rather than from something the browser assembled, and
   there is no client-side copy of the ranking that could drift from the real
   one.

   Without JavaScript none of this runs and nothing breaks — the dropdown
   simply never opens, and the Search button below still turns up the full
   result list with an add button on each row. */
(function () {
  "use strict";

  function init() {
    var picker = document.querySelector(".list-add-picker");
    var form = document.getElementById("list-add-pick-form");
    if (!picker || !form) {
      return;
    }

    var input = picker.querySelector("input[type=search]");
    var list = picker.querySelector(".suggest-list");
    var tmdbField = form.querySelector('input[name="tmdb_id"]');
    var filmField = form.querySelector('input[name="film_id"]');
    var queryField = form.querySelector('input[name="q"]');
    var suggestUrl = picker.getAttribute("data-suggest-url");
    if (!input || !list || !suggestUrl) {
      return;
    }

    var timer = null;
    var items = [];
    var active = -1;

    function hide() {
      list.hidden = true;
      list.innerHTML = "";
      items = [];
      active = -1;
      input.setAttribute("aria-expanded", "false");
    }

    function setActive(index) {
      if (!items.length) {
        return;
      }
      items.forEach(function (el, i) {
        el.classList.toggle("active", i === index);
      });
      active = index;
      items[index].scrollIntoView({ block: "nearest" });
    }

    // Picking a row is a write, so the two states that must not write are
    // refused here rather than left to the route: a row already in the list,
    // and a row carrying no identifier the add route could act on.
    function pick(row) {
      if (!row || row.in_list) {
        return;
      }
      queryField.value = input.value.trim();
      if (row.tmdb_id !== null && row.tmdb_id !== undefined) {
        tmdbField.value = String(row.tmdb_id);
      } else if (row.film_id !== null && row.film_id !== undefined) {
        filmField.value = String(row.film_id);
      } else {
        return;
      }
      form.submit();
    }

    function render(results) {
      list.innerHTML = "";
      items = [];
      active = -1;
      if (!results.length) {
        hide();
        return;
      }
      results.forEach(function (row) {
        var li = document.createElement("li");
        var el = document.createElement("button");
        el.type = "button";
        el.className = "suggest-row" + (row.in_list ? " suggest-row-in-list" : "");

        if (row.poster_url) {
          var img = document.createElement("img");
          img.className = "suggest-thumb";
          img.src = row.poster_url;
          img.alt = "";
          el.appendChild(img);
        } else {
          var ph = document.createElement("span");
          ph.className = "suggest-thumb suggest-thumb-placeholder";
          el.appendChild(ph);
        }

        var label = document.createElement("span");
        label.className = "suggest-row-title";
        label.textContent = row.title + (row.year ? " (" + row.year + ")" : "");
        el.appendChild(label);

        if (row.in_list) {
          var state = document.createElement("span");
          state.className = "suggest-row-state";
          state.textContent = "In this list";
          el.appendChild(state);
          el.disabled = true;
        } else {
          el.addEventListener("click", function () {
            pick(row);
          });
        }

        li.appendChild(el);
        list.appendChild(li);
        items.push(el);
      });
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }

    input.addEventListener("input", function () {
      if (timer) {
        clearTimeout(timer);
      }
      var q = input.value.trim();
      if (q.length < 2) {
        hide();
        return;
      }
      // Debounced so a word costs a few requests, not one per keystroke —
      // the same courtesy the header box pays the shared TMDB quota.
      timer = setTimeout(function () {
        fetch(suggestUrl + "?q=" + encodeURIComponent(q), {
          credentials: "same-origin",
        })
          .then(function (resp) {
            return resp.ok ? resp.json() : { results: [] };
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
        // With nothing highlighted, Enter keeps its default and submits the
        // search form below — the same split the header box uses, so the
        // typeahead never steals the ordinary search.
        e.preventDefault();
        var target = items[active];
        if (!target.disabled) {
          target.click();
        }
      }
    });

    document.addEventListener("click", function (e) {
      if (!picker.contains(e.target)) {
        hide();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
