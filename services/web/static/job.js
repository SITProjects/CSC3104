// Job page: follow the job live over server-sent events and draw the result.
// All text is set with textContent, never innerHTML, because node names and
// error messages come from outside this page.
(() => {
  const root = document.getElementById("job");
  if (!root) return;
  const jobId = root.dataset.jobId;
  const $ = (id) => document.getElementById(id);

  const STATUS_TEXT = {
    PREPARING: "Preparing", PENDING: "Queued", RUNNING: "Running", VOTING: "Checking",
    VERIFIED: "Verified", DISPUTED: "Disputed", COMPLETED: "Completed",
    FAILED: "Failed", CANCELLED: "Cancelled", SCHEDULED: "Queued",
    WAITING: "Waiting for a PC", EVICTED: "Moving to another PC",
    LOST: "Moving to another PC", DONE: "Done",
  };
  const label = (s) => STATUS_TEXT[s] || s;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function badge(status) {
    return el("span", `badge badge-${status.toLowerCase()}`, label(status));
  }

  function bar(value) {
    const wrap = el("div", "mini-progress");
    const p = el("progress");
    p.max = 100;
    p.value = value;
    p.setAttribute("aria-label", `${value}%`);
    wrap.append(p, el("span", "hint", `${value}%`));
    return wrap;
  }

  // The coordinator calls a copy RUNNING as soon as its Kubernetes Job exists,
  // even before any PC has picked it up. Without a PC it is still waiting.
  function copyState(r) {
    if (r.status === "RUNNING" && !r.node) return "WAITING";
    if (r.status === "SCHEDULED") return "PENDING";
    return r.status;
  }

  function copyItem(r) {
    const item = el("li", "copy");
    item.append(el("span", "node", r.node || "–"), " · ", badge(copyState(r)));
    if (r.status === "RUNNING" && r.node) item.append(` ${r.progress}%`);
    if (r.restarts > 0) {
      // counts every relaunch: an eviction, a lost pod or a failed launch
      const n = r.restarts;
      item.append(el("span", "restart", ` ↻ ${n} ${n === 1 ? "retry" : "retries"}`));
    }
    return item;
  }

  // A task only counts as running once one of its copies has a PC.
  function taskState(task) {
    if (task.status !== "RUNNING") return task.status;
    const started = task.replicas.some((r) => r.node || r.status === "DONE");
    return started ? "RUNNING" : "PENDING";
  }

  function voteText(task) {
    const v = task.votes;
    if (!v) return "";
    if (task.status === "VERIFIED") return `${v.agree} of ${v.voted} agree`;
    if (task.status === "DISPUTED") return "No majority";
    return `${v.voted} of ${v.copies} in`;
  }

  function renderTask(task) {
    const row = el("tr");
    row.append(el("th", null, `#${task.index + 1}`));
    row.firstChild.scope = "row";
    const status = el("td");
    status.append(badge(taskState(task)));
    const progress = el("td");
    progress.append(bar(task.progress));
    const copies = el("td");
    const list = el("ul", "copies");
    task.replicas.forEach((r) => list.append(copyItem(r)));
    copies.append(list);
    row.append(status, progress, copies, el("td", null, voteText(task)));
    return row;
  }

  function render(job) {
    const badgeEl = $("status-badge");
    badgeEl.className = `badge badge-${job.status.toLowerCase()}`;
    badgeEl.textContent = label(job.status);

    $("overall").value = job.progress;
    const err = $("job-error");
    err.hidden = !job.error;
    err.textContent = job.error || "";

    let text;
    if (job.stage === "preparing") text = "Preparing your pages. This usually takes under a minute.";
    else if (job.stage === "failed") text = "This upload could not be processed.";
    else if (job.status === "COMPLETED") text = "Done. Your text is ready to download.";
    else if (job.status === "CANCELLED") text = "This job was cancelled.";
    else if (job.status === "FAILED") text = `${job.progress}% done before the job failed.`;
    else text = `${job.progress}% complete`;
    $("overall-text").textContent = text;
    if (job.stage === "preparing") $("overall").removeAttribute("value");

    const hasTasks = job.tasks.length > 0;
    $("stats").hidden = !hasTasks;
    $("tasks-section").hidden = !hasTasks;
    if (hasTasks) {
      $("stat-verified").textContent = `${job.tasks_verified} / ${job.tasks_total}`;
      $("stat-restarts").textContent = job.restarts;
      $("stat-disputed").textContent = job.tasks_disputed;
      $("task-rows").replaceChildren(...job.tasks.map(renderTask));
    }
    $("download").hidden = job.status !== "COMPLETED";
    // Cancel works while preparing too. A failed upload can only be dismissed.
    const dismiss = job.stage === "failed";
    $("cancel-form").hidden = job.terminal && !dismiss;
    $("cancel-button").textContent = dismiss ? "Dismiss" : "Cancel job";
    $("cancel-button").dataset.mode = dismiss ? "dismiss" : "cancel";
  }

  $("cancel-form").addEventListener("submit", (e) => {
    const asking = $("cancel-button").dataset.mode === "cancel";
    if (asking && !confirm("Cancel this job? Work already done is discarded.")) {
      e.preventDefault();
    }
  });

  function connect() {
    const source = new EventSource(`/jobs/${jobId}/stream`);
    const note = $("connection");
    source.onmessage = (e) => {
      note.textContent = "";
      render(JSON.parse(e.data));
    };
    source.onerror = () => {
      // EventSource reconnects by itself; just tell the user
      note.textContent = "Connection lost. Reconnecting…";
    };
    source.addEventListener("done", () => {
      source.close();
      note.textContent = "";
    });
    source.addEventListener("gone", () => {
      source.close();
      $("status-badge").textContent = "Not found";
      $("overall-text").textContent = "We couldn't find this job. It may belong to another account.";
    });
    source.addEventListener("auth", () => {
      source.close();
      window.location.assign(`/login?next=${encodeURIComponent(location.pathname)}`);
    });
  }

  connect();
})();
