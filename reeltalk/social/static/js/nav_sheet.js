/* Mobile navigation behaviour (N3). Deliberately small vanilla JS per R6 --
   no framework, no build step. Two jobs: the More sheet (a native dialog,
   so Escape and focus containment come free -- this adds the open/close
   wiring, the aria-expanded state, and the focus back to the trigger) and
   focusing the search page's mobile field when it is the visible one. */
(function () {
  "use strict";

  function initSheet() {
    var more = document.querySelector(".more-button");
    var sheet = document.getElementById("more-sheet");
    if (!more || !sheet || typeof sheet.showModal !== "function") {
      return;
    }
    more.addEventListener("click", function () {
      sheet.showModal();
      more.setAttribute("aria-expanded", "true");
    });
    sheet.querySelector(".more-sheet-close").addEventListener("click", function () {
      sheet.close();
    });
    // A click on the backdrop lands on the dialog box itself, not a child.
    sheet.addEventListener("click", function (e) {
      if (e.target === sheet) {
        sheet.close();
      }
    });
    // Explicit focus cycling, as the approved mockup shipped: native
    // containment covers the middle of the list, but Tab off the last row
    // could land on the body behind the sheet before wrapping; wrapping
    // first and last here makes the cycle deterministic.
    sheet.addEventListener("keydown", function (e) {
      if (e.key !== "Tab") {
        return;
      }
      var items = Array.prototype.slice
        .call(sheet.querySelectorAll("a[href],button:not([disabled])"))
        .filter(function (el) {
          return el.getClientRects().length;
        });
      if (!items.length) {
        return;
      }
      var first = items[0];
      var last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    });
    // Escape closes natively; every close path lands here, so the trigger's
    // state and the focus are restored in one place.
    sheet.addEventListener("close", function () {
      more.setAttribute("aria-expanded", "false");
      more.focus();
    });
  }

  function initSearchFocus() {
    var input = document.querySelector(".page-search input[type=search]");
    // offsetParent is null while the field is display:none on desktop, so a
    // member using the header box keeps their own focus behaviour.
    if (input && input.offsetParent !== null && !input.value) {
      input.focus();
    }
  }

  function init() {
    initSheet();
    initSearchFocus();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
