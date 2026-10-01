/*
 * Landing page language dropdown (app/i18n.py, 2026-09-16 - rebuilt
 * 2026-09-16 from a native <select> into a custom button+listbox: a
 * <select> can't render a flag image inside its <option>s, and flag
 * emoji specifically render as literal "GB"/"ID" text on Windows/
 * Chromium rather than actual flags. CSP is script-src 'self' with no
 * inline-script/onclick allowance (app/middleware.py), so all of this
 * lives here as a same-origin script.
 *
 * Server-side rendering only - this never swaps text client-side.
 * Selecting an option navigates to "/?lang=<value>"; app/main.py's
 * home() route reads that query param, sets the as4pur_lang cookie, and
 * re-renders the whole page (including this dropdown's own trigger)
 * from app/i18n.py's LANDING_TRANSLATIONS.
 *
 * Standard "combobox trigger + listbox popup" pattern: options keep
 * tabindex="-1" permanently (never in the normal tab order) - arrow-key
 * navigation moves DOM focus between them programmatically via
 * .focus(), which works on a tabindex="-1" element even though Tab
 * itself would skip it. This is the same approach ARIA authoring
 * practice examples use for listbox popups.
 *
 * Focus-after-click race, found via test_landing_lang_dropdown.py
 * (deterministic 3/3 failures against a real Chromium, not test flake):
 * calling options[0].focus() synchronously inside the trigger's own
 * click handler can still lose to Chromium's OWN default focus
 * assignment for the <button> that was actually clicked - a browser
 * assigns focus to a clicked element as part of its default mousedown
 * handling, which can land AFTER this handler finishes running,
 * silently stealing focus back to the trigger a moment later. A next,
 * fast keydown (a real double-action, or - the case that surfaced this -
 * Playwright's click()+keyboard.press() with no real-world delay between
 * them) then hits the trigger instead of the option, and Enter on a
 * plain <button> fires its own click (toggling the panel shut) rather
 * than this file's option-selection logic - explaining the exact
 * symptom: no navigation happened at all. (A setTimeout(fn, 0)
 * deferral was tried first and made things WORSE - a new race against
 * Playwright's own next scripted action, this time non-deterministic
 * instead of a clean reproducible failure.) The actual fix: a
 * `mousedown` listener on the trigger that calls preventDefault() -
 * the standard way to stop an element from receiving the browser's
 * default focus-on-click at all, so this file's own focusOption() call
 * in the `click` handler below has no competing native behavior left to
 * lose to. `click` still fires normally afterward; only the native
 * default *focus* action tied to mousedown is suppressed.
 */
(function () {
  "use strict";

  var dropdown = document.querySelector(".lang-dropdown");
  if (!dropdown) return;

  var trigger = dropdown.querySelector(".lang-dropdown-trigger");
  var panel = dropdown.querySelector(".lang-dropdown-panel");
  var options = Array.prototype.slice.call(dropdown.querySelectorAll(".lang-dropdown-option"));

  function isOpen() {
    return !panel.hidden;
  }

  function open() {
    panel.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
  }

  function close(returnFocusToTrigger) {
    panel.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    if (returnFocusToTrigger) trigger.focus();
  }

  function selectLanguage(code) {
    window.location.href = "/?lang=" + encodeURIComponent(code);
  }

  function focusOption(index) {
    var clamped = (index + options.length) % options.length;
    options[clamped].focus();
  }

  function indexOf(el) {
    return options.indexOf(el);
  }

  function focusActiveOption() {
    var current = dropdown.querySelector('.lang-dropdown-option[aria-selected="true"]') || options[0];
    focusOption(indexOf(current));
  }

  // See this file's header comment on the focus-after-click race - this
  // is the actual fix (a setTimeout deferral was tried and made things
  // worse). Suppresses only the browser's default focus-on-mousedown
  // for the trigger button itself; `click` still fires normally.
  trigger.addEventListener("mousedown", function (event) {
    event.preventDefault();
  });

  trigger.addEventListener("click", function () {
    if (isOpen()) {
      close(false);
    } else {
      open();
      focusActiveOption();
    }
  });

  trigger.addEventListener("keydown", function (event) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!isOpen()) open();
      focusActiveOption();
    }
  });

  options.forEach(function (option, i) {
    option.addEventListener("click", function () {
      selectLanguage(option.getAttribute("data-lang"));
    });
    option.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        focusOption(i + 1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        focusOption(i - 1);
      } else if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        selectLanguage(option.getAttribute("data-lang"));
      } else if (event.key === "Escape") {
        event.preventDefault();
        close(true);
      } else if (event.key === "Tab") {
        close(false);
      }
    });
  });

  document.addEventListener("click", function (event) {
    if (isOpen() && !dropdown.contains(event.target)) close(false);
  });
})();
