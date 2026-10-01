// Polls /jobs/{id}/status.json every 2s while a job is pending/running, and
// updates the progress bar and counts in place. Falls back to a slower
// retry on a network hiccup rather than giving up. Used by
// templates/jobs/status.html.
(function () {
  var script = document.currentScript;
  var jobId = script.getAttribute("data-job-id");
  var statusText = document.getElementById("job-status-text");
  var progress = document.getElementById("job-progress");
  var counts = document.getElementById("job-counts");
  var total = document.getElementById("job-total");

  var ACTIVE_STATUSES = ["pending", "running"];

  function poll() {
    fetch("/jobs/" + jobId + "/status.json", { credentials: "same-origin" })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (data.error) return;
        statusText.textContent = data.status;
        progress.max = data.total_items || 1;
        progress.value = data.processed_items;
        total.textContent = data.total_items;
        counts.textContent = data.success_count + " succeeded · " + data.skipped_count + " skipped · " + data.failed_count + " failed";
        if (ACTIVE_STATUSES.indexOf(data.status) !== -1) {
          setTimeout(poll, 2000);
        } else {
          // Final state reached - reload once to pick up the export link
          // and any error message rendered server-side.
          location.reload();
        }
      })
      .catch(function () {
        setTimeout(poll, 5000);
      });
  }

  if (ACTIVE_STATUSES.indexOf(statusText.textContent.trim()) !== -1) {
    setTimeout(poll, 2000);
  }
})();
