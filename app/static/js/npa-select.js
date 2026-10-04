// "Users per NPA policy" - the policy list (templates/operations/data_export/npa_select.html).
//
// Keeps three things in step with the ticked boxes, nothing else:
//   - the live "N of M selected" summary in the sticky toolbar (a role=status region, so a screen reader hears it),
//   - the count on the Continue button, and
//   - the button itself: disabled while nothing is ticked.
//
// Nothing here changes what is submitted. The page works without this script: the button is enabled in the markup,
// and the server still rejects an empty choice. list-filter.js (loaded first) owns the filter box and "Select all";
// it sets `checked` on the rows without firing a change event per row, so this listens for `change` on the whole
// form: an event reaches the form after the control's own handlers have run, whichever script was loaded first.
(function () {
  var form = document.querySelector("form.de-form");
  var list = document.getElementById("npa-policy-list");
  if (!form || !list) return;

  var summaryN = form.querySelector("[data-selected-n]");
  var summaryM = form.querySelector("[data-selected-m]");
  var hiddenNote = form.querySelector("[data-selected-hidden]");
  var countLabel = form.querySelector("[data-selected-count]");
  var hint = form.querySelector("[data-bar-hint]");
  var button = form.querySelector(".de-actionbar button[type=submit]");

  function rows() {
    return Array.prototype.slice.call(list.querySelectorAll(".publisher-row"));
  }

  function update() {
    var all = rows();
    var ticked = all.filter(function (row) {
      var box = row.querySelector("input[type=checkbox]");
      return box && box.checked;
    });
    // A row hidden by the filter stays ticked and is still submitted, so it still counts - and is said out loud.
    var hiddenTicked = ticked.filter(function (row) { return row.hidden; }).length;

    if (summaryN) summaryN.textContent = String(ticked.length);
    if (summaryM) summaryM.textContent = String(all.length);
    if (hiddenNote) {
      hiddenNote.textContent = hiddenTicked ? " (" + hiddenTicked + " hidden by the filter)" : "";
      hiddenNote.hidden = !hiddenTicked;
    }
    if (countLabel) countLabel.textContent = ticked.length ? "(" + ticked.length + ")" : "";
    if (button) button.disabled = ticked.length === 0;
    if (hint) hint.textContent = ticked.length ? "Next: a preview of what the export will do." : "Select at least one policy to continue.";
  }

  form.addEventListener("change", update);
  form.addEventListener("input", update);               // typing in the filter can hide ticked rows
  window.addEventListener("pageshow", update);          // Back/forward restores ticked boxes without a load
  update();
})();
