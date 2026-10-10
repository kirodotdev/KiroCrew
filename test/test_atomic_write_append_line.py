"""``append_line`` and its call sites when the disk fills mid-append (D54).

A write that runs out of room keeps the bytes the kernel accepted and fails on the
rest. The site tests reproduce that for real: the append runs in a child process
whose file-size limit (``RLIMIT_FSIZE``) ends partway through the new line, so the
kernel takes the first part and the next write fails. That is EFBIG rather than
ENOSPC, with the same short write a full disk makes. The parent then appends with
no limit, as when space comes back, and reads the file back. All state is under
``tmp_path``, and the child gets ``KIROCREW_HOME`` pointed there too.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import subprocess
import sys
import textwrap

import pytest

import kiro_crew.atomic_write as aw
from kiro_crew.cron_history import CronHistoryStore, CronRunRecord
from kiro_crew.history import ConversationLog

KEY = "chat-1-1790000000"

_CHILD = textwrap.dedent("""
    import asyncio, json, resource, signal, sys
    from pathlib import Path
    kind, base, limit = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
    if kind == "transcript":
        from kiro_crew.history import ConversationLog
        target = ConversationLog(base_dir=base)
    else:
        from kiro_crew.cron_history import CronHistoryStore, CronRunRecord
        target = CronHistoryStore(base_dir=base)
        record = CronRunRecord(job_id="job-full", started_at=1.0, summary="s" * 400)
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    try:
        if kind == "transcript":
            target.append(sys.argv[4], "assistant", "x" * 400)
        else:
            asyncio.run(target.append(record))
    except OSError as exc:
        print(json.dumps({"errno": exc.errno}))
    else:
        print(json.dumps({"errno": None}))
    """)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_FSIZE is POSIX")


def _append_with_space_running_out(tmp_path, kind: str, limit: int) -> dict:
    """Run one append in a child whose file-size limit is *limit* bytes."""
    env = {**os.environ, "KIROCREW_HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run(
        [sys.executable, "-c", _CHILD, kind, str(tmp_path / kind), str(limit), KEY],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


@posix_only
def test_a_failed_transcript_append_does_not_swallow_the_next_message(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    log = ConversationLog(base_dir=tmp_path / "transcript")
    log.append(KEY, "user", "first message")
    transcript = next((tmp_path / "transcript").rglob(f"{KEY}.jsonl"))
    before = transcript.stat().st_size
    child = _append_with_space_running_out(tmp_path, "transcript", before + 100)
    after_failure = transcript.stat().st_size
    log = ConversationLog(base_dir=tmp_path / "transcript")  # space is back
    log.append(KEY, "user", "third message, written after space returned")
    texts = [m.get("content") for m in log.read_messages(KEY)]
    assert "third message, written after space returned" in texts, (
        f"the first message written after space returned was lost: read back {texts}; "
        f"the failed append left the transcript at {after_failure} bytes (was {before})"
    )
    assert child["errno"] == errno.EFBIG, f"the caller must still see the failure: {child}"
    assert after_failure == before, "a failed append must leave no partial line behind"


@posix_only
@pytest.mark.asyncio
async def test_a_failed_cron_run_append_does_not_swallow_the_next_run(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    store = CronHistoryStore(base_dir=tmp_path / "cron")
    first = CronRunRecord(job_id="job-full", started_at=0.0, summary="run 0")
    await store.append(first)
    job_file = tmp_path / "cron" / "cron-history" / "job-full.jsonl"
    before = job_file.stat().st_size
    child = await asyncio.to_thread(_append_with_space_running_out, tmp_path, "cron", before + 60)
    after_failure = job_file.stat().st_size
    store = CronHistoryStore(base_dir=tmp_path / "cron")  # space is back
    later = CronRunRecord(job_id="job-full", started_at=2.0, summary="run 2")
    await store.append(later)
    rows, total = await store.get_job_history("job-full", 0, 50)
    listed = [row.get("run_id") for row in rows]
    detail = await store.get_run_detail("job-full", later.run_id)
    assert later.run_id in listed and detail is not None, (
        f"the first run recorded after space returned was lost: job history lists {listed} "
        f"(total {total}); run detail = {detail!r}; the failed append left the job file at "
        f"{after_failure} bytes (was {before})"
    )
    # The store keeps its "costs one record" contract: the failure is logged, not raised.
    assert child["errno"] is None
    assert after_failure == before, "a failed append must leave no partial line behind"


def _half_then_enospc(fd, data, path):
    os.write(fd, data[: len(data) // 2])
    raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))


def test_append_line_takes_back_a_failed_write(tmp_path, monkeypatch):
    target = tmp_path / "log.jsonl"
    target.write_bytes(b'{"n": 1}\n')
    with monkeypatch.context() as m:
        m.setattr(aw, "_write_all", _half_then_enospc)
        with pytest.raises(OSError) as raised:
            aw.append_line(target, json.dumps({"n": 2}))
    assert raised.value.errno == errno.ENOSPC
    assert target.read_bytes() == b'{"n": 1}\n', "the file must be back at its old size"
    aw.append_line(target, json.dumps({"n": 3}))
    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert rows == [{"n": 1}, {"n": 3}]


def test_append_line_starts_a_fresh_line_after_a_torn_tail(tmp_path):
    target = tmp_path / "log.jsonl"
    target.write_bytes(b'{"n": 1}\n{"n": 2, "to')  # torn by a kill: no handler ran
    aw.append_line(target, json.dumps({"n": 3}))
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[0] == '{"n": 1}'
    assert json.loads(lines[-1]) == {"n": 3}, f"the new record was glued to the torn tail: {lines}"


def test_append_line_writes_what_a_text_mode_append_writes(tmp_path):
    """Control: on a healthy file the bytes and the creation mode are unchanged."""
    builtin, helper = tmp_path / "builtin.jsonl", tmp_path / "helper.jsonl"
    for line in ('{"n": 1}', '{"text": "caf\\u00e9 \u2713"}'):
        with open(builtin, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        aw.append_line(helper, line)
    assert helper.read_bytes() == builtin.read_bytes()
    assert (helper.stat().st_mode & 0o777) == (builtin.stat().st_mode & 0o777)


def test_atomic_write_keeps_the_old_file_and_raises_on_enospc(tmp_path, monkeypatch):
    """Control for the temp-file + rename stores (cron jobs, state.json, lessons)."""
    target = tmp_path / "store.json"
    target.write_text('{"version": 1}', encoding="utf-8")

    def full(*_a, **_kw):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    with monkeypatch.context() as m:
        m.setattr(aw, "_write_all", full)
        with pytest.raises(OSError) as raised:
            aw.atomic_write(target, '{"version": 2}')
    assert raised.value.errno == errno.ENOSPC
    assert target.read_text(encoding="utf-8") == '{"version": 1}'
    assert sorted(p.name for p in tmp_path.iterdir()) == ["store.json"], "temp file left behind"
    aw.atomic_write(target, '{"version": 2}')
    assert target.read_text(encoding="utf-8") == '{"version": 2}'
