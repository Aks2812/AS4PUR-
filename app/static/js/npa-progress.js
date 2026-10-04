// "Users per NPA policy" - the run page (templates/operations/data_export/npa_run.html).
//
// The bar is a native <progress> so the shared job-poll.js can keep driving it through `.value` and `.max`. A native
// <progress> already reports its value to assistive technology; the explicit role and aria-value* attributes in the
// markup say the same thing out loud, and this keeps them equal to what job-poll.js writes. Setting `.value` or `.max`
// changes the matching content attribute, which is what is observed here. job-poll.js itself is not touched.
(function () {
  var bar = document.getElementById("job-progress");
  if (!bar || typeof MutationObserver === "undefined") return;

  function sync() {
    var max = Number(bar.getAttribute("max")) || 1;
    var value = Number(bar.getAttribute("value")) || 0;
    bar.setAttribute("aria-valuemin", "0");
    bar.setAttribute("aria-valuemax", String(max));
    bar.setAttribute("aria-valuenow", String(value));
    // Only what is known: how many steps are finished, out of the total the job reported.
    bar.setAttribute("aria-valuetext", value === 0 ? "No steps finished yet" : value + " of " + max + " steps");
  }

  new MutationObserver(sync).observe(bar, { attributes: true, attributeFilter: ["max", "value"] });
  sync();
})();
