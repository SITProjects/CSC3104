// Live updates with Server-Sent Events. An element with data-stream="<url>"
// gets its contents replaced by each HTML message the server sends.
document.querySelectorAll("[data-stream]").forEach((element) => {
  const source = new EventSource(element.dataset.stream);
  // Outside the element, because every message replaces the element's contents
  const notice = document.createElement("p");
  notice.className = "alert";
  notice.hidden = true;
  element.before(notice);
  let finished = false;

  source.onopen = () => {
    notice.hidden = true;
  };
  source.onmessage = (event) => {
    element.innerHTML = event.data;
  };
  source.onerror = () => {
    if (finished) return;
    // CLOSED means the browser gave up; otherwise it is already retrying
    notice.textContent =
      source.readyState === EventSource.CLOSED
        ? "Live updates stopped. Reload the page to see the latest status."
        : "Connection lost. Reconnecting…";
    notice.hidden = false;
  };
  // The server sends "done" once the job has ended, so stop reconnecting
  source.addEventListener("done", () => {
    finished = true;
    source.close();
  });
});

// A form with data-confirm asks first. Listening on the document also covers
// forms that a live update swapped in.
document.addEventListener("submit", (event) => {
  const message = event.target.dataset.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});

// Check the file before uploading 50 MB just to have the server refuse it
const form = document.getElementById("upload-form");
if (form) {
  const button = form.querySelector("button[type=submit]");
  const label = button.textContent;
  form.addEventListener("submit", (event) => {
    const file = form.elements.pdf.files[0];
    const error = document.getElementById("upload-error");
    const maxBytes = Number(form.dataset.maxBytes);
    let message = "";
    if (file && file.size > maxBytes) {
      message = `The PDF is over ${Math.round(maxBytes / 1048576)} MB.`;
    } else if (file && !file.name.toLowerCase().endsWith(".pdf")) {
      message = "Only PDF files are accepted.";
    }
    if (message) {
      event.preventDefault();
      error.textContent = message;
      error.hidden = false;
      return;
    }
    button.disabled = true;
    button.textContent = "Uploading…";
  });
  // The Back button restores the page as it was, with the button still disabled
  window.addEventListener("pageshow", () => {
    button.disabled = false;
    button.textContent = label;
  });
}
