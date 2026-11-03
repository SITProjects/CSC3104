// Asks before submitting any form with a data-confirm question, such as
// removing a worker. Without this script the forms still work, unasked.
(() => {
  for (const form of document.querySelectorAll("form[data-confirm]")) {
    form.addEventListener("submit", (e) => {
      if (!confirm(form.dataset.confirm)) e.preventDefault();
    });
  }
})();
