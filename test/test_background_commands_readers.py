"""Background commands: the pure readers over a command's retained files.

The exit-status and log-tail readers (each held to the file it opened, with no
private-key body in a tail), the redaction of the retained command line, and the
timeout clamp. The spawn and settle lifecycle is pinned in
``test_background_commands.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import kiro_crew.background_commands as bg


def test_the_status_reader_takes_the_last_whole_line(tmp_path):
    status = tmp_path / "exit_status"
    status.write_text("0\n4\n")
    fd = os.open(status, os.O_RDONLY)
    try:
        status_id = bg._file_id(fd)
    finally:
        os.close(fd)

    assert bg._read_exit_status(status, status_id)[0] == "4"
    with open(status, "a") as tail:
        tail.write("7")
    assert bg._read_exit_status(status, status_id)[0] == "4"
    with open(status, "a") as tail:
        tail.write("\nforged\n")
    assert bg._read_exit_status(status, status_id)[0] is None


def test_inline_secrets_are_redacted_from_the_retained_command():
    for secret_command in (
        "curl -u admin:hunter2secret https://example.com",
        "PGPASSWORD=hunter2secret psql -h db",
        "git clone https://bot:hunter2secret@example.com/repo",
        "mysql --password=hunter2secret db",
        "cli --token hunter2secret run",
        "git clone https://bot:p@ss@hunter2secret@example.com/repo",
        "password=hunter2secret ./run",
        "db_pass=hunter2secret ./run",
        "curl -u admin:\\\nhunter2secret https://example.com",
    ):
        assert "hunter2secret" not in bg._redact_command(secret_command), secret_command
    for spaced in (
        "curl -u 'admin:hunter2 xsecret' https://example.com",
        'curl -u "admin:hunter2 xsecret" https://example.com',
        "curl --user='admin:hunter2 xsecret' https://example.com",
        'curl -u admin:"hunter2 xsecret" https://example.com',
        "curl -u admin:hunter2\\ xsecret https://example.com",
        'PGPASSWORD="hunter2 xsecret" psql -h db',
        "mysql --password 'hunter2 xsecret' db",
    ):
        shown = bg._redact_command(spaced)
        assert "hunter2" not in shown and "xsecret" not in shown, spaced
    for plain in (
        "gh pr checks 7 --watch --fail-fast",
        "docker run -p 8080:80 img",
        "echo passes=3 tokens=12",
        "npm test -- --passWithNoTests",
        "git push -u origin main",
    ):
        assert bg._redact_command(plain) == plain


def test_a_replaced_or_linked_log_is_never_read(tmp_path):
    log = tmp_path / "output.log"
    log.write_text("mine\n")
    fd = os.open(log, os.O_RDONLY)
    try:
        log_id = bg._file_id(fd)
    finally:
        os.close(fd)
    assert bg._read_tail(log, log_id)[0] == "mine"

    secret = tmp_path / "secret.txt"
    secret.write_text("not yours\n")
    # Kept linked, so the forged file below cannot reuse its inode number.
    log.rename(tmp_path / "original.log")
    log.symlink_to(secret)
    assert bg._read_tail(log, log_id) == ("", 0)

    log.unlink()
    log.write_text("forged\n")
    assert bg._read_tail(log, log_id) == ("", 0)


# Assembled, so no source line carries a whole private-key header.
_KEY_KIND = "RSA PRIVATE" + " KEY"


def _pem(key: int) -> str:
    body = "".join(f"K{key:02d}L{line:02d}{'A' * 58}\n" for line in range(60))
    return f"-----BEGIN {_KEY_KIND}-----\n{body}-----END {_KEY_KIND}-----\n"


def _owned_log(path: Path, text: str) -> str:
    path.write_text(text)
    fd = os.open(path, os.O_RDONLY)
    try:
        return bg._file_id(fd)
    finally:
        os.close(fd)


def test_the_tail_never_carries_a_private_key_body(tmp_path):
    # Longer than the kept lines, so cutting before redacting drops its header.
    one = tmp_path / "one.log"
    tail, _ = bg._read_tail(one, _owned_log(one, "start\n" + _pem(0) + "done\n"))
    assert "A" * 10 not in tail and tail.endswith("done")

    # Past the read window, which then opens inside a key whose header it misses.
    keys = "".join(_pem(key) for key in range(20))
    many = tmp_path / "many.log"
    log_id = _owned_log(many, keys + "done\n")
    start = many.stat().st_size - bg._TAIL_READ_BYTES
    assert keys.rfind("-----BEGIN", 0, start) > keys.rfind("-----END", 0, start)
    tail, _ = bg._read_tail(many, log_id)
    assert "A" * 10 not in tail and tail.endswith("done")


def test_timeout_is_defaulted_and_bounded():
    assert bg.clamp_timeout(None) == bg.DEFAULT_TIMEOUT_SECS
    assert bg.clamp_timeout(True) == bg.DEFAULT_TIMEOUT_SECS
    assert bg.clamp_timeout(1) == bg.MIN_TIMEOUT_SECS
    assert bg.clamp_timeout(10**9) == bg.MAX_TIMEOUT_SECS
