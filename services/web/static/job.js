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
    FAILED: "Failed", CANCELLED: "Cancelled", SCHEDULED: "Starting",
    EVICTED: "Moving", LOST: "Moving", DONE: "Done",
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

  function copyItem(r) {
    const item = el("li", "copy");
    item.append(el("span", "node", r.node || "waiting for a PC"), " · ", badge(r.status));
    if (r.status === "RUNNING") item.append(` ${r.progress}%`);
    if (r.restarts > 0) {
      const n = r.restarts;
      item.append(el("span", "restart", ` ↻ ${n} restart${n === 1 ? "" : "s"}`));
    }
    return item;
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
    status.append(badge(task.status));
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
    // only once the coordinator has the job; before that there is nothing to cancel
    $("cancel-form").hidden = job.terminal || job.stage !== "active";
  }

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
