/*
 * Login page interactions (docs/design-system.md Sections 16/20,
 * 2026-09-16). CSP is script-src 'self' with no inline-script/
 * onclick allowance (app/middleware.py), so both the password
 * show/hide toggle and the submit-loading state live here as a small
 * same-origin script rather than inline handlers.
 */
(function () {
  "use strict";

  var passwordInput = document.getElementById("login-password");
  var toggle = document.querySelector(".login-password-toggle");
  if (passwordInput && toggle) {
    toggle.addEventListener("click", function () {
      var showing = passwordInput.type === "text";
      passwordInput.type = showing ? "password" : "text";
      toggle.setAttribute("aria-label", showing ? "Show password" : "Hide password");
    });
  }

  // Scoped to the login form specifically, not a page-wide selector -
  // CLAUDE.md Section 12's button[type=submit] lesson (the persistent
  // logout form in the header shares that selector on every other page)
  // doesn't apply here since login.html has no sidebar/logout form to
  // collide with, but the same scoping discipline is followed anyway.
  var form = document.querySelector(".login-form");
  var submitButton = form ? form.querySelector("button[type='submit']") : null;
  if (form && submitButton) {
    form.addEventListener("submit", function () {
      submitButton.disabled = true;
      submitButton.innerHTML =
        '<span class="login-spinner" aria-hidden="true"></span> Signing in...';
    });
  }
})();
