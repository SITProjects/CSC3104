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
    LOST: "Moving to another PC", DONE: "Done", SCORING: "Scoring",
    PULLING: "Downloading image", PULL_FAILED: "Can't download image",
  };
  const label = (s) => STATUS_TEXT[s] || s;

  // Keep the colours in step with status_badge in templates/_macros.html.
  const TONE = {
    RUNNING: "bg-blue-100 text-blue-800", SCHEDULED: "bg-blue-100 text-blue-800",
    PREPARING: "bg-blue-100 text-blue-800", VOTING: "bg-blue-100 text-blue-800",
    SCORING: "bg-blue-100 text-blue-800", PULLING: "bg-blue-100 text-blue-800",
    COMPLETED: "bg-green-100 text-green-800", VERIFIED: "bg-green-100 text-green-800",
    DONE: "bg-green-100 text-green-800",
    EVICTED: "bg-amber-100 text-amber-800", LOST: "bg-amber-100 text-amber-800",
    PULL_FAILED: "bg-amber-100 text-amber-800",
  };
  const DESTRUCTIVE = new Set(["FAILED", "DISPUTED", "CANCELLED"]);
  // Job types with something to download, and when the button shows
  const DOWNLOADS = {
    ocr: { file: "result.txt", what: "text", when: ["COMPLETED"] },
    python: { file: "output.tar.gz", what: "output", when: ["COMPLETED", "FAILED"] },
    pytorch: { file: "output.tar.gz", what: "output", when: ["COMPLETED", "FAILED"] },
    "pytorch-gpu": { file: "output.tar.gz", what: "output", when: ["COMPLETED", "FAILED"] },
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function styleBadge(node, status) {
    node.className = `badge ${TONE[status] || ""}`.trim();
    node.dataset.variant = DESTRUCTIVE.has(status) ? "destructive" : "secondary";
    node.textContent = label(status);
    return node;
  }

  const badge = (status) => styleBadge(el("span"), status);

  function bar(value) {
    const wrap = el("div", "flex min-w-32 items-center gap-2");
    const p = el("progress", "h-1.5 w-full appearance-none overflow-hidden rounded-full bg-muted [&::-webkit-progress-bar]:bg-muted [&::-webkit-progress-value]:bg-primary [&::-moz-progress-bar]:bg-primary");
    p.max = 100;
    p.value = value;
    p.setAttribute("aria-label", `${value}%`);
    wrap.append(p, el("span", "text-xs tabular-nums text-muted-foreground", `${value}%`));
    return wrap;
  }

  // The coordinator calls a copy RUNNING as soon as its Kubernetes Job exists,
  // even before any PC has picked it up. Without a PC it is still waiting; with
  // one, the PC may first have to download the job's image (several GB for GPU
  // jobs), which the coordinator reports as pulling_since or pull_error.
  function copyState(r) {
    if (r.status === "RUNNING" && !r.node) return "WAITING";
    if (r.status === "RUNNING" && r.pull_error) return "PULL_FAILED";
    if (r.status === "RUNNING" && r.pulling_since) return "PULLING";
    if (r.status === "SCHEDULED") return "PENDING";
    return r.status;
  }

  // "just now", "4 min ago", "2 h ago"
  function ago(iso) {
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    return `${Math.floor(s / 3600)} h ago`;
  }

  function copyItem(r) {
    const item = el("li", "flex flex-wrap items-center gap-1.5");
    // a check copy runs on the platform's checker, not on a lending PC
    const who = r.check ? `checker${r.node ? ` (${r.node})` : ""}` : r.node || "–";
    const state = copyState(r);
    item.append(el("span", "font-medium", who), " · ", badge(state));
    if (state === "RUNNING") item.append(` ${r.progress}%`);
    if (state === "PULLING" || state === "PULL_FAILED") {
      // the kubelet keeps retrying a failed pull, so both show how long it has been
      if (r.pulling_since) {
        const since = el("span", "text-xs text-muted-foreground", `started ${ago(r.pulling_since)}`);
        since.title = new Date(r.pulling_since).toLocaleString();
        item.append(since);
      }
      if (r.pull_error) {
        const why = el("span", "text-xs text-amber-700", "retrying");
        why.title = r.pull_error;
        item.append(why);
      }
    }
    if (r.saved_at && (r.status === "RUNNING" || r.status === "SCHEDULED")) {
      // a moved copy carries on from here; older work is redone
      const step = r.saved_step != null ? `, step ${r.saved_step}` : "";
      const saved = el("span", "text-xs text-muted-foreground", `saved ${ago(r.saved_at)}${step}`);
      saved.title = new Date(r.saved_at).toLocaleString();
      item.append(saved);
    }
    if (r.restarts > 0) {
      // counts every relaunch: an eviction, a lost pod or a failed launch
      const n = r.restarts;
      item.append(el("span", "text-xs text-amber-700", `↻ ${n} ${n === 1 ? "retry" : "retries"}`));
    }
    return item;
  }

  // A task only counts as running once one of its copies has a PC, and while
  // its PCs are all still downloading the image it says so instead.
  const DOWNLOADING = ["PULLING", "PULL_FAILED"];
  function taskState(task) {
    if (task.status !== "RUNNING") return task.status;
    const copies = task.replicas.map(copyState);
    if (!copies.some((s) => s === "RUNNING" || s === "DONE")) {
      const downloading = DOWNLOADING.find((s) => copies.includes(s));
      if (downloading) return downloading;
    }
    const started = task.replicas.some((r) => r.node || r.status === "DONE");
    return started ? "RUNNING" : "PENDING";
  }

  // The job's badge: a running job whose tasks are all still queued or
  // downloading the image says it is downloading.
  function jobState(job) {
    if (job.status !== "RUNNING") return job.status;
    const tasks = job.tasks.map(taskState);
    if (!tasks.every((s) => s === "PENDING" || DOWNLOADING.includes(s))) return job.status;
    return DOWNLOADING.find((s) => tasks.includes(s)) || job.status;
  }

  // The job's stages, in order; see the stages list in templates/job_detail.html.
  const STAGE_TEXT = { done: "done", current: "in progress", todo: "not started" };
  const CHECKING = new Set(["VOTING", "SCORING", "VERIFIED", "DISPUTED", "FAILED"]);

  // Which stage the job is at now.
  function currentStage(job) {
    if (job.stage === "preparing" || job.stage === "failed") return "prepare";
    if (job.status === "COMPLETED") return "done";
    if (!job.tasks.some((t) => taskState(t) !== "PENDING")) return "queue";
    return job.tasks.every((t) => CHECKING.has(t.status)) ? "check" : "run";
  }

  function renderStages(job) {
    $("stage-prepare").hidden = job.job_type !== "ocr";
    const steps = [...document.querySelectorAll("#stages > li:not([hidden])")];
    const at = steps.findIndex((li) => li.dataset.step === currentStage(job));
    const stopped = job.status === "FAILED" || job.status === "CANCELLED";
    steps.forEach((li, i) => {
      let state = i < at ? "done" : i > at ? "todo" : "current";
      if (state === "current" && stopped) state = "failed";
      if (state === "current" && li.dataset.step === "done") state = "done";
      li.dataset.state = state;
      if (state === "current" || state === "failed") li.setAttribute("aria-current", "step");
      else li.removeAttribute("aria-current");
      li.querySelector("[data-number]").textContent = i + 1;
      li.querySelector("[data-state-label]").textContent =
        state === "failed" ? label(job.status).toLowerCase() : STAGE_TEXT[state];
    });
  }

  // The platform's score of a trained model, in place of a vote
  function scoreCell(s) {
    const cell = el("td", "grid gap-0.5");
    if (s.state === "awaiting") {
      cell.append(el("span", null, "Awaiting score"));
      return cell;
    }
    if (s.state === "not_scored") {
      cell.append(el("span", null, "Not scored"));
      if (s.reason) cell.append(el("span", "text-xs text-muted-foreground wrap-anywhere", s.reason));
      return cell;
    }
    const pct = (x) => `${(x * 100).toFixed(1)}%`;
    cell.append(el("span", "font-medium", `${pct(s.score)} accuracy`));
    cell.append(el("span", "text-xs text-muted-foreground", `on ${s.samples} samples, measured by the platform`));
    if (s.target != null) {
      cell.append(el("span", s.met ? "text-xs text-green-700" : "text-xs text-amber-700",
        `Target ${pct(s.target)} ${s.met ? "met" : "not met"}`));
    }
    const digest = el("span", "font-mono text-xs text-muted-foreground", `model ${s.model_sha256.slice(0, 12)}…`);
    digest.title = `SHA-256 of model.onnx: ${s.model_sha256}`;
    cell.append(digest);
    return cell;
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
    row.append(el("th", "tabular-nums", `#${task.index + 1}`));
    row.firstChild.scope = "row";
    const status = el("td");
    status.append(badge(taskState(task)));
    const progress = el("td");
    progress.append(bar(task.progress));
    const copies = el("td");
    const list = el("ul", "grid gap-1.5");
    task.replicas.forEach((r) => list.append(copyItem(r)));
    copies.append(list);
    row.append(status, progress, copies, task.score ? scoreCell(task.score) : el("td", null, voteText(task)));
    return row;
  }

  function render(job) {
    const download = DOWNLOADS[job.job_type];
    styleBadge($("status-badge"), jobState(job));
    renderStages(job);

    $("overall").value = job.progress;
    const err = $("job-error");
    err.hidden = !job.error;
    $("job-error-text").textContent = job.error || "";

    let text;
    if (job.stage === "preparing") text = "Preparing your pages. This usually takes under a minute.";
    else if (job.stage === "failed") text = "This upload could not be processed.";
    else if (job.status === "COMPLETED")
      text = download ? `Done. Your ${download.what} is ready to download.` : "Done.";
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
    // OCR jobs return text. Generic jobs return their output and log, even
    // when the command failed, so the log can be read.
    const link = $("download");
    link.hidden = !download || !download.when.includes(job.status);
    if (download) {
      link.href = `/jobs/${jobId}/${download.file}`;
      link.lastChild.textContent = ` Download ${download.what}`;
    }
    // Cancel works while preparing too. A failed upload can only be dismissed.
    const dismiss = job.stage === "failed";
    $("cancel-form").hidden = job.terminal && !dismiss;
    $("cancel-button").textContent = dismiss ? "Dismiss" : "Cancel job";
    $("cancel-button").dataset.mode = dismiss ? "dismiss" : "cancel";
    const gone = Boolean(job.files_deleted_at);
    $("delete-files-form").hidden = !job.terminal || dismiss || gone;
    if (gone) {
      link.hidden = true;
      $("overall-text").textContent = (job.status === "COMPLETED" ? "Done." : text)
        + " Its files have been deleted.";
    }
  }

  $("cancel-form").addEventListener("submit", (e) => {
    const asking = $("cancel-button").dataset.mode === "cancel";
    if (asking && !confirm("Cancel this job? Work already done is discarded.")) {
      e.preventDefault();
    }
  });

  $("delete-files-form").addEventListener("submit", (e) => {
    if (!confirm("Delete this job's files? You can't download them afterwards.")) {
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
