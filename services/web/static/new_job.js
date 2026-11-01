// New job page: show the fields the chosen job type needs (a PDF, or a command
// and files), check a PDF before sending it, show upload progress, and put any
// server error on the page.
// Without JavaScript the form still posts normally and the server checks it all.
(() => {
  const form = document.getElementById("job-form");
  if (!form) return;
  const $ = (id) => document.getElementById(id);
  const typeSelect = $("job_type");
  const typeHint = $("job-type-hint");
  const pdfSection = $("pdf-section");
  const resources = $("resources-section");
  const commandSection = $("command-section");
  const input = $("file");
  const title = $("file-title");
  const errorBox = $("form-error");
  const errorText = $("form-error-text");
  const submit = $("job-submit");
  const progress = $("upload-progress");
  const bar = $("upload-bar");
  const status = $("upload-status");
  const maxBytes = Number(form.dataset.maxBytes);
  const maxMb = form.dataset.maxMb;

  const showError = (message) => {
    errorText.textContent = message;
    errorBox.hidden = !message;
  };
  const size = (bytes) =>
    bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.ceil(bytes / 1024)} KB`;
  const chosen = () => typeSelect.selectedOptions[0]?.dataset ?? {};
  const needsPdf = () => chosen().needsPdf === "true";

  // Hidden fields are also disabled, so they are neither checked nor sent.
  function showFieldsFor() {
    const type = chosen();
    if (type.description) typeHint.textContent = type.description;
    if (type.example) $("command").placeholder = type.example;
    pdfSection.hidden = type.needsPdf !== "true";
    input.disabled = pdfSection.hidden;
    input.required = !pdfSection.hidden;
    commandSection.hidden = type.runsCommand !== "true";
    for (const field of commandSection.querySelectorAll("input")) {
      field.disabled = commandSection.hidden;
    }
    $("command").required = !commandSection.hidden;
    resources.hidden = type.customResources !== "true";
    for (const field of resources.querySelectorAll("input")) {
      field.disabled = resources.hidden;
      field.placeholder = `Default: ${type[field.name] ?? ""}`;
    }
  }
  typeSelect.addEventListener("change", () => {
    showFieldsFor();
    showError("");
  });
  showFieldsFor();

  // Returns a problem with the chosen file, or "" if it looks fine.
  const problem = (file) => {
    if (!file) return "Choose a PDF to upload.";
    if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf")
      return "That file is not a PDF.";
    if (file.size > maxBytes) return `That file is over ${maxMb} MB.`;
    return "";
  };

  input.addEventListener("change", () => {
    const file = input.files[0];
    title.textContent = file ? `${file.name} (${size(file.size)})` : "Choose a PDF";
    showError(file ? problem(file) : "");
  });

  form.addEventListener("submit", (event) => {
    if (!typeSelect.value) {
      event.preventDefault();
      showError("Choose a job type.");
      return;
    }
    if (!form.checkValidity()) {
      event.preventDefault();
      form.reportValidity();
      return;
    }
    // A job without an upload is quick, so let the browser post it as usual.
    if (!needsPdf()) {
      submit.disabled = true;
      return;
    }
    const message = problem(input.files[0]);
    event.preventDefault();
    if (message) {
      showError(message);
      return;
    }
    showError("");
    submit.disabled = true;
    progress.hidden = false;
    bar.removeAttribute("value"); // indeterminate until the first progress event

    const xhr = new XMLHttpRequest();
    xhr.open("POST", form.action);
    xhr.upload.addEventListener("progress", (e) => {
      if (!e.lengthComputable) return;
      const percent = Math.round((e.loaded / e.total) * 100);
      bar.value = percent;
      status.textContent =
        percent < 100 ? `Uploading… ${percent}%` : "Checking your PDF…";
    });
    xhr.addEventListener("load", () => {
      // the server answers success with a redirect to the job page, which XHR follows
      if (xhr.status === 200 && /\/jobs\/[0-9a-f-]{36}$/.test(xhr.responseURL)) {
        window.location.assign(xhr.responseURL);
        return;
      }
      if (xhr.responseURL.includes("/login")) {
        window.location.assign(xhr.responseURL);
        return;
      }
      const doc = new DOMParser().parseFromString(xhr.responseText, "text/html");
      const alert = doc.querySelector("#form-error:not([hidden])");
      fail(alert ? alert.textContent.trim() : "The upload failed. Please try again.");
    });
    xhr.addEventListener("error", () => fail("The upload was interrupted. Please try again."));
    xhr.send(new FormData(form));
  });

  function fail(message) {
    showError(message);
    submit.disabled = false;
    progress.hidden = true;
    bar.value = 0;
  }
})();
