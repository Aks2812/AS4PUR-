// Generic live client-side filter for a list of rows already on the
// page - no new API calls, just hides non-matching rows as the operator
// types. Built for Stage B's private-app selection list (RTP Creation:
// real tenants can have 100+ apps, an unfiltered scrollable checkbox
// list isn't usable at that scale), but written generically so any
// .publisher-list-shaped list (publishers, policy groups, apps) can opt
// in the same way:
//
//   <div class="list-filter">
//     <input type="search" data-filter-target="app-list" placeholder="Filter by name...">
//   </div>
//   <div class="publisher-list" id="app-list">
//     <label class="publisher-row">...</label>
//     ...
//   </div>
//   <p class="list-filter-empty" data-filter-empty hidden>No matches.</p>
//   <script src="/static/js/list-filter.js"></script>
//
// Optional "select all" master checkbox (2026-09-13) - opt in on any
// CHECKBOX-based (not radio-based) list above by adding, anywhere on the
// page, a checkbox targeting the same list id:
//
//   <label class="select-all-row">
//     <input type="checkbox" data-select-all-target="app-list">
//     Select all (<span data-select-all-count>0</span> shown)
//   </label>
//
// Works with or without a companion [data-filter-target] search box on
// the same list - with no filter present, every row simply counts as
// "shown". Checking the master checkbox checks only the rows currently
// visible (row.hidden === false) and never touches hidden ones -
// selecting everything genuinely means clearing the search box first,
// matching this app's review-gate philosophy of never acting on
// anything the operator can't currently see. The master checkbox's own
// state (checked / unchecked / indeterminate) re-syncs on every filter
// change AND on every individual row checkbox toggle, so it always
// reflects the current visible selection rather than just the last
// action taken on the master itself.
document.addEventListener("DOMContentLoaded", function () {
  // Shared per-list state so the search filter and the "select all"
  // master checkbox (when both target the same list) stay in sync
  // without either one needing to know the other exists.
  var listStates = {};

  function getListState(listId) {
    if (Object.prototype.hasOwnProperty.call(listStates, listId)) {
      return listStates[listId];
    }
    var list = document.getElementById(listId);
    var state = list
      ? {
          rows: Array.prototype.slice.call(list.querySelectorAll(".publisher-row")),
          master: null,
          countLabel: null,
        }
      : null;
    listStates[listId] = state;
    return state;
  }

  function rowCheckbox(row) {
    return row.querySelector("input[type=checkbox]");
  }

  function syncMasterState(state) {
    if (!state || !state.master) return;
    var visibleCheckboxes = state.rows
      .filter(function (row) {
        return !row.hidden;
      })
      .map(rowCheckbox)
      .filter(Boolean);
    var checkedCount = visibleCheckboxes.filter(function (cb) {
      return cb.checked;
    }).length;

    if (state.countLabel) {
      state.countLabel.textContent = String(visibleCheckboxes.length);
    }
    if (visibleCheckboxes.length === 0 || checkedCount === 0) {
      state.master.checked = false;
      state.master.indeterminate = false;
    } else if (checkedCount === visibleCheckboxes.length) {
      state.master.checked = true;
      state.master.indeterminate = false;
    } else {
      state.master.checked = false;
      state.master.indeterminate = true;
    }
  }

  // --- Search filter ---
  document.querySelectorAll("[data-filter-target]").forEach(function (input) {
    var state = getListState(input.getAttribute("data-filter-target"));
    if (!state) return;
    var emptyMsg = document.querySelector("[data-filter-empty]");

    input.addEventListener("input", function () {
      var query = input.value.trim().toLowerCase();
      var visibleCount = 0;
      state.rows.forEach(function (row) {
        var text = (row.textContent || "").toLowerCase();
        var matches = !query || text.indexOf(query) !== -1;
        // Belt-and-suspenders against a real bug found during real-tenant
        // UI testing: .publisher-row sets `display: flex` directly, which
        // (being author CSS) silently beats the browser's default
        // `[hidden] { display: none }` rule regardless of a companion
        // override existing elsewhere in style.css - confirmed to
        // reproduce the exact "typing does nothing" symptom if that
        // override is ever missing (e.g. a future stylesheet refactor).
        // Setting inline style directly here needs no companion CSS rule
        // to exist at all, so this class of bug can't recur.
        row.hidden = !matches;
        row.style.display = matches ? "" : "none";
        if (matches) visibleCount++;
      });
      if (emptyMsg) emptyMsg.hidden = visibleCount !== 0;
      // Filtering only ever changes visibility, never selection state -
      // this call re-derives the master checkbox's checked/indeterminate
      // state from whatever is still actually checked among the rows
      // that remain visible; it never un-checks a now-hidden row.
      syncMasterState(state);
    });
  });

  // --- "Select all" master checkbox ---
  document.querySelectorAll("[data-select-all-target]").forEach(function (master) {
    var state = getListState(master.getAttribute("data-select-all-target"));
    if (!state) return;
    state.master = master;
    var labelEl = master.closest("label");
    state.countLabel = labelEl ? labelEl.querySelector("[data-select-all-count]") : null;

    state.rows.forEach(function (row) {
      var cb = rowCheckbox(row);
      if (!cb) return;
      // Re-sync the master's own state whenever any individual row is
      // toggled by hand, not just when the master itself is clicked -
      // this is what keeps checked/unchecked/indeterminate accurate
      // after a partial manual selection.
      cb.addEventListener("change", function () {
        syncMasterState(state);
      });
    });

    master.addEventListener("change", function () {
      var checked = master.checked;
      state.rows.forEach(function (row) {
        if (row.hidden) return; // never touch what's currently filtered out
        var cb = rowCheckbox(row);
        if (cb) cb.checked = checked;
      });
      syncMasterState(state);
    });

    syncMasterState(state); // reflect any pre-checked rows (e.g. via Back) immediately on load
  });
});
