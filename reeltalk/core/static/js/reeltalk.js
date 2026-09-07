/* ReelTalk front-end behaviour (M1 increment 5). Deliberately small vanilla
   JS per R6 — no framework, no build step. Two jobs: toggle the finish-flow
   modal and drive the clickable half-star rating widget. */
(function () {
  "use strict";

  function init() {
    // --- Modal open/close -------------------------------------------------
    document.querySelectorAll("[data-open-modal]").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        var modal = document.getElementById(btn.getAttribute("data-open-modal"));
        if (modal) {
          modal.hidden = false;
        }
      });
    });

    // Close on backdrop click or the [hidden] reset.
    document.querySelectorAll(".modal").forEach(function (modal) {
      modal.addEventListener("click", function (e) {
        if (e.target === modal || e.target.classList.contains("modal-backdrop")) {
          modal.hidden = true;
        }
      });
    });

    // --- Clickable star rating (0.5–5, half steps) ------------------------
    document.querySelectorAll(".star-rating").forEach(function (widget) {
      var fg = widget.querySelector(".stars-fg");
      var input = widget.querySelector('input[name="rating"]');
      var hits = widget.querySelector(".star-hits");
      var form = widget.closest("form");
      var submit = form ? form.querySelector('button[type="submit"]') : null;

      function setRating(value) {
        input.value = String(value);
        fg.style.width = (value / 5 * 100) + "%";
        if (submit && value) {
          submit.disabled = false;
        }
      }

      // Ten half-star hit zones across the five stars: zone i sets (i+1)*0.5.
      for (var i = 0; i < 10; i++) {
        (function (value) {
          var hit = document.createElement("span");
          hit.setAttribute("data-value", String(value));
          hit.addEventListener("click", function () {
            setRating(value);
          });
          hits.appendChild(hit);
        })((i + 1) * 0.5);
      }

      var initial = widget.getAttribute("data-value");
      if (initial) {
        setRating(parseFloat(initial));
      } else if (submit) {
        submit.disabled = true;
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
