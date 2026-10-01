// Generic "I have reviewed the results above" gate (CLAUDE.md Section 6):
// a checkbox marked [data-confirm-gate] enables the nearest form's submit
// button only once checked. Built once here so every operation's
// review-before-you-proceed page just adds the checkbox + button markup,
// e.g.:
//
//   <form method="post" action="...">
//     ...
//     <label class="review-gate">
//       <input type="checkbox" data-confirm-gate>
//       I have reviewed the results above.
//     </label>
//     <button type="submit">Proceed</button>
//   </form>
//   <script src="/static/js/confirm-gate.js"></script>
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("[data-confirm-gate]").forEach(function (checkbox) {
    var form = checkbox.closest("form");
    if (!form) return;
    var submitBtn = form.querySelector('[type="submit"]');
    if (!submitBtn) return;
    submitBtn.disabled = true;
    checkbox.addEventListener("change", function () {
      submitBtn.disabled = !checkbox.checked;
    });
  });
});
