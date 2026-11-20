"""The runner: out-of-memory notes (issue #199) and progress reports (issue #207)."""

import importlib.util
import signal
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / "runner.py"


@pytest.fixture
def runner(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("runner_under_test", RUNNER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["runner_under_test"] = module
    spec.loader.exec_module(module)
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    real_path = module.Path

    def fake_path(p):
        p = str(p)
        if p.startswith("/sys/fs/cgroup/"):
            return real_path(cgroup / p.removeprefix("/sys/fs/cgroup/"))
        return real_path(p)

    monkeypatch.setattr(module, "Path", fake_path)
    module.cgroup = cgroup
    return module


def events(runner, kills: int) -> None:
    (runner.cgroup / "memory.events").write_text(
        f"low 0\nhigh 0\nmax 12\noom 1\noom_kill {kills}\n"
    )


def test_killed_by_the_oom_killer_says_so(runner):
    events(runner, 0)
    (runner.cgroup / "memory.max").write_text(f"{4096 * 2**20}\n")
    before = runner.oom_kills()
    events(runner, 1)
    log = runner.Log()
    runner.note_out_of_memory(-signal.SIGKILL, before, log)
    text = log.value().decode()
    assert "ran out of memory" in text and "4096 MiB" in text


def test_other_kills_and_exits_say_nothing(runner):
    events(runner, 0)
    log = runner.Log()
    runner.note_out_of_memory(-signal.SIGKILL, 0, log)  # no new OOM kill
    runner.note_out_of_memory(1, 0, log)  # an ordinary failure
    assert log.value() == b""


def test_no_limit_or_unreadable_cgroup(runner):
    assert runner.oom_kills() is None
    (runner.cgroup / "memory.max").write_text("max\n")
    assert runner.memory_limit_mib() is None
    events(runner, 2)
    log = runner.Log()
    runner.note_out_of_memory(-signal.SIGKILL, None, log)
    assert "ran out of memory" in log.value().decode()
    assert "MiB" not in log.value().decode()


def test_read_progress(runner, tmp_path):
    path = tmp_path / "progress"
    assert runner.read_progress(path) is None  # not written yet
    for text, value in (
        ("42.7\n", 42),
        ("100", 100),
        ("150", 100),
        ("-3", 0),
        ("7 of 10", 7),
        ("", None),
        ("half", None),
        ("nan", None),
    ):
        path.write_text(text)
        assert runner.read_progress(path) == value, text


def test_progress_is_sent_only_when_it_changes(runner, tmp_path, monkeypatch):
    import threading

    path = tmp_path / "progress"
    path.write_text("10")
    sent = []
    done = threading.Event()

    def fake_call(config, method, where, body=None, **kwargs):
        sent.append(body["progress"])
        if len(sent) == 1:
            path.write_text("10.9")  # same whole number: not sent again
        elif len(sent) == 2:
            done.set()

    def change_soon():
        threading.Event().wait(0.05)
        path.write_text("55")

    monkeypatch.setattr(runner, "call", fake_call)
    threading.Thread(target=change_soon).start()
    runner.report_progress_every({}, path, 0.01, done)
    assert sent == [10, 55]


def test_output_progress_from_epoch_lines(runner):
    out = runner.OutputProgress()
    assert out.value() is None
    out.feed(b"[11:49:06] Epoch 1/30: loss 0.8468, test accuracy 92.15%\n")
    assert out.value() == 3
    out.feed(b"[11:50:21] Epoch 11/30: loss 0.0514\n")
    assert out.value() == 36
    out.feed(b"epoch 3 of 30\n")  # going back is ignored
    assert out.value() == 36
    out.feed(b"Epoch 30/30: done\n")  # stays below 100 until the copy is done
    assert out.value() == 99


def test_output_progress_from_tqdm(runner):
    out = runner.OutputProgress()
    out.feed(
        b" 10%|#         | 10/100 [00:01<00:09]\r 45%|####5     | 45/100 [00:04<00:05]\n"
    )
    assert out.value() == 45


def test_counted_lines_beat_tqdm(runner):
    out = runner.OutputProgress()
    out.feed(b"Epoch 2/10\n")
    out.feed(b"100%|##########| 500/500 [00:05<00:00]\n")  # an inner bar per epoch
    assert out.value() == 20


def test_other_text_is_ignored(runner):
    out = runner.OutputProgress()
    for line in (
        b"Downloaded 3/10 files\n",
        b"accuracy 98.5%\n",
        b"epoch 5/0\n",
        b"Epoch 12/10\n",
    ):
        out.feed(line)
    assert out.value() is None


def test_progress_file_wins_over_output(runner, tmp_path, monkeypatch):
    import threading

    path = tmp_path / "progress"
    out = runner.OutputProgress()
    out.feed(b"Epoch 5/10\n")
    sent = []
    done = threading.Event()

    def fake_call(config, method, where, body=None, **kwargs):
        sent.append(body["progress"])
        if len(sent) == 1:
            path.write_text("80")
        else:
            done.set()

    monkeypatch.setattr(runner, "call", fake_call)
    runner.report_progress_every({}, path, 0.01, done, out)
    assert sent == [50, 80]
