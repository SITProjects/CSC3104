# Evaluation

Owner: all members; each owner contributes experiments for their component.

Place experiment and plotting scripts here. Use `data/` for shared sample PDFs,
`results/` for raw CSV measurements, and `graphs/` for report figures. These
artifacts remain trackable so results can be reproduced.

Use the same samples across comparisons, run experiments at least three times,
and plot averages with error bars as specified in the implementation plan.
Record experiment parameters and reproduction commands alongside the scripts.

## Job timeline

`job_timeline.sql` shows where one job's time went (upload, job created, first
copy launched, last copy finished, completed) and how it was split (pages, tasks,
pages per task, copies, distinct nodes). Usage is in the file's header. Record
each run's numbers in `results/`.
