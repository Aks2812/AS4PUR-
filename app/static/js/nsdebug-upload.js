/*
 * nsdebug.log upload page: size pre-check before the upload starts.
 *
 * The limit comes from the server (form[data-max-bytes], config
 * nsdebug_upload_max_bytes) so this never drifts from what the server
 * enforces - the server-side check stays the authority; this just saves a
 * multi-megabyte upload that would be rejected anyway. External file, not
 * inline, because the CSP allows script-src 'self' only.
 */
(function () {
  var form = document.querySelector("form[data-max-bytes]");
  var banner = document.getElementById("nsdebug-upload-client-error");
  var input = form && form.querySelector('input[type="file"]');
  var maxBytes = form ? parseInt(form.getAttribute("data-max-bytes"), 10) : 0;
  if (!form || !banner || !input || !(maxBytes > 0)) return;

  // Same wording and number format as the server's own message
  // (routes.py _upload_rejection_message / uploads.format_size).
  function formatSize(bytes) {
    var MB = 1024 * 1024;
    var value, unit;
    if (bytes >= MB) { value = bytes / MB; unit = "MB"; }
    else if (bytes >= 1024) { value = bytes / 1024; unit = "KB"; }
    else return bytes + " bytes";
    return value.toFixed(1).replace(/\.0$/, "") + " " + unit;
  }

  function check() {
    var file = input.files && input.files[0];
    if (file && file.size > maxBytes) {
      banner.textContent =
        "This file is " + formatSize(file.size) + ", which is over the " + formatSize(maxBytes) +
        " upload limit. Collect a fresh log, or trim it to the relevant time window, and try again.";
      banner.hidden = false;
      input.value = ""; // nothing left to submit until a smaller file is chosen
      return false;
    }
    banner.hidden = true;
    banner.textContent = "";
    return true;
  }

  input.addEventListener("change", check);
  form.addEventListener("submit", function (event) {
    if (!check()) event.preventDefault();
  });
})();
