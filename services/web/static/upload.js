// Upload page: check the file before sending, show upload progress, and put any
// server error on the page. Without JavaScript the form still posts normally.
(() => {
  const form = document.getElementById("upload-form");
  if (!form) return;
  const input = document.getElementById("file");
  const title = document.getElementById("file-title");
  const errorBox = document.getElementById("upload-error");
  const submit = document.getElementById("upload-submit");
  const progress = document.getElementById("upload-progress");
  const bar = document.getElementById("upload-bar");
  const status = document.getElementById("upload-status");
  const maxBytes = Number(form.dataset.maxBytes);
  const maxMb = form.dataset.maxMb;

  const showError = (message) => {
    errorBox.textContent = message;
    errorBox.hidden = !message;
  };
  const size = (bytes) =>
    bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.ceil(bytes / 1024)} KB`;

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
    const file = input.files[0];
    const message = problem(file);
    if (message) {
      event.preventDefault();
      showError(message);
      return;
    }
    event.preventDefault();
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
      const alert = doc.querySelector(".alert");
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
