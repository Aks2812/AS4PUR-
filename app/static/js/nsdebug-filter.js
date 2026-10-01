// Client-side filter chips + evidence "show more" toggle for the
// nsdebug.log "Per-check results" table (Device Posture Validation upload
// sub-feature). Rows/evidence are already fully rendered server-side
// (service.py build_report_view) - this only ever toggles visibility, no
// new API calls, consistent with this app's minimal-JS convention:
//
//   <button data-status-filter="ALL">All</button>
//   <button data-status-filter="failed">FAILED</button>
//   ...
//   <tr data-status-row="failed">...</tr>
//
//   <li class="evidence-hidden" hidden>...</li>
//   <li><button data-evidence-toggle>Show 147 more lines</button></li>
document.addEventListener("DOMContentLoaded", function () {
  var chips = document.querySelectorAll("[data-status-filter]");
  var rows = document.querySelectorAll("[data-status-row]");

  chips.forEach(function (chip) {
    chip.addEventListener("click", function () {
      var target = chip.getAttribute("data-status-filter");
      chips.forEach(function (c) {
        c.classList.remove("active");
      });
      chip.classList.add("active");
      rows.forEach(function (row) {
        row.hidden = target !== "ALL" && row.getAttribute("data-status-row") !== target;
      });
    });
  });

  document.querySelectorAll("[data-evidence-toggle]").forEach(function (button) {
    button.addEventListener("click", function () {
      var list = button.closest("ul");
      if (!list) return;
      list.querySelectorAll(".evidence-hidden").forEach(function (li) {
        li.hidden = false;
      });
      var toggleRow = button.closest("li");
      if (toggleRow) toggleRow.remove();
    });
  });
});
