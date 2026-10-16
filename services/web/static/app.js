'use strict';

// Template preview only. No API requests, uploads, storage writes, or pipeline work.
// TODO: Connect this form to the upload API in the separate pipeline PR.
const form = document.querySelector('#upload-form');
if (form) {
  const input = document.querySelector('#pdf-file');
  const zone = document.querySelector('#drop-zone');
  const feedback = document.querySelector('#upload-error');
  const showFeedback = (message, success = false) => {
    feedback.className = `alert ${success ? 'alert-success' : 'alert-danger'} mt-3`;
    feedback.setAttribute('role', success ? 'status' : 'alert');
    feedback.textContent = message;
  };
  const updateFile = () => {
    const file = input.files[0];
    document.querySelector('#file-info').textContent = file ? `${file.name} · ${(file.size / 1000000).toFixed(2)} MB` : '';
    feedback.classList.add('d-none');
  };
  input.addEventListener('change', updateFile);
  for (const name of ['dragenter', 'dragover']) zone.addEventListener(name, event => {
    event.preventDefault(); zone.classList.add('dragging');
  });
  for (const name of ['dragleave', 'drop']) zone.addEventListener(name, event => {
    event.preventDefault(); zone.classList.remove('dragging');
  });
  zone.addEventListener('drop', event => {
    if (event.dataTransfer.files.length !== 1) return showFeedback('Choose one PDF at a time.');
    input.files = event.dataTransfer.files;
    updateFile();
  });
  const checkSelection = event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const file = input.files[0];
    if (!file || !file.name.toLowerCase().endsWith('.pdf')) return showFeedback('Choose a file with the .pdf extension.');
    if (file.size === 0) return showFeedback('This file is empty. Choose a PDF that contains pages.');
    if (file.size > Number(form.dataset.limit)) return showFeedback(`This PDF exceeds the ${Number(form.dataset.limit) / 1000000} MB preview limit.`);
    // TODO: Validate actual PDF contents, encryption, and page count on the server.
    showFeedback('Selection checked. Nothing has been uploaded or saved. Upload and processing will be connected in a later PR.', true);
  };
  form.addEventListener('submit', checkSelection);
  document.querySelector('#submit-upload').addEventListener('click', checkSelection);
  form.addEventListener('reset', () => {
    feedback.classList.add('d-none');
    document.querySelector('#file-info').textContent = '';
  });
}

// Filtering and pagination operate only on the rows supplied to the template.
// TODO: Populate these views from the implemented job API when it is connected.
const filterForm = document.querySelector('#job-filter-form');
if (filterForm) {
  const filter = document.querySelector('#status-filter');
  const rows = Array.from(document.querySelectorAll('tr[data-job-status]'));
  const previous = document.querySelector('#previous-page');
  const next = document.querySelector('#next-page');
  const parameters = new URLSearchParams(window.location.search);
  const selected = parameters.get('status') || '';
  if (Array.from(filter.options).some(option => option.value === selected)) filter.value = selected;
  let page = Math.max(1, Number.parseInt(parameters.get('page'), 10) || 1);
  const pageSize = 5;
  function render(updateUrl = true) {
    const matches = rows.filter(row => !filter.value || row.dataset.jobStatus === filter.value);
    const pages = Math.max(1, Math.ceil(matches.length / pageSize));
    page = Math.min(Math.max(1, page), pages);
    const visible = new Set(matches.slice((page - 1) * pageSize, page * pageSize));
    rows.forEach(row => { row.hidden = !visible.has(row); });
    document.querySelector('#job-count').textContent = matches.length;
    document.querySelector('#pagination-label').textContent = `Page ${page} of ${pages} · ${matches.length} jobs`;
    document.querySelector('#filter-empty').classList.toggle('d-none', matches.length !== 0);
    previous.disabled = page <= 1;
    next.disabled = page >= pages;
    if (updateUrl) {
      const query = new URLSearchParams();
      if (filter.value) query.set('status', filter.value);
      if (page > 1) query.set('page', page);
      const search = query.toString();
      const url = `${window.location.pathname}${search ? `?${search}` : ''}`;
      try { window.history.replaceState(null, '', url); } catch { /* file:// previews may restrict history. */ }
    }
  }
  filterForm.addEventListener('submit', event => { event.preventDefault(); page = 1; render(); });
  filter.addEventListener('change', () => { page = 1; render(); });
  previous.addEventListener('click', () => { page -= 1; render(); });
  next.addEventListener('click', () => { page += 1; render(); });
  document.querySelector('#reset-filter').addEventListener('click', () => { filter.value = ''; page = 1; render(); });
  render(false);
}

// TODO: Connect live progress only after the public job endpoint is implemented.
// TODO: Connect downloads only after verified output is published by the verifier.
// TODO: Populate node and cost views from existing read-only data sources.
const dateFormat = new Intl.DateTimeFormat('en-SG', {
  day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
  hour12: false, timeZone: 'Asia/Singapore'
});
for (const time of document.querySelectorAll('time[datetime]')) {
  const date = new Date(time.dateTime);
  if (!Number.isNaN(date.valueOf())) time.textContent = `${dateFormat.format(date)} SGT`;
}
