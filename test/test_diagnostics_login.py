"""The OS login is scrubbed from a diagnostics bundle member in every spelling.

The home collapse takes the login where it is a segment of a home path, including
the Windows 8.3 short spelling of the home; ``diagnostics_login`` takes it where it
stands as a word of its own. Both run inside ``diagnostics._scrub_bundle``, so
these tests drive that whole pipeline on one member's text.
"""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew import diagnostics


def test_bundle_replaces_the_login_where_it_stands_as_its_own_word(monkeypatch):
    """The OS login outside any path goes too, as ``<user>``, but only whole.

    ``<login>@<host>``, ``user=<login>``, a Windows path folder and a line of
    serialized or coloured output lose it in any case; a name the login only
    starts or ends (``jo2``, ``jo.doe``, ``dev-jo``, ``ann.jo``) stays, by the
    boundary the home collapse keeps for a sibling login, and so do the letter of a
    serialized ``\\n`` in front of a login that starts with ``n`` and a folder that
    letter starts.
    """
    login = "nathansmith"
    home = "/" + "home" + "/" + login  # assembled so no literal home path is on a line
    monkeypatch.setattr(diagnostics.Path, "home", classmethod(lambda cls: Path(home)))
    monkeypatch.setattr(diagnostics, "config_dir", lambda: Path(home + "/.kiro/crew"))
    monkeypatch.setattr(diagnostics.diagnostics_host_name, "host_names", lambda: [])
    kept = (
        f"{login}2 {login}.doe dev-{login} dev-{login}-box ann.{login} "
        + '"done\\'
        + login
        + '" D:\\Work\\n'
        + login
        + "\\x"
    )

    out, _, n = diagnostics._scrub_bundle(
        f"tunnel 127.0.0.1:2222 -> {login}@example-host\n"
        f"user={login.upper()}\n"
        f"open D:\\Work\\{login}\\notes.txt\n"
        '{"out": "USER PID\\n' + login + ' 42", "prompt": "\x1b[32m' + login + '@box"}\n'
        f"{home}/.kiro/crew/gateway.log\n"
        f"{kept}\n"
    )

    assert out == (
        "tunnel 127.0.0.1:2222 -> <user>@example-host\n"
        "user=<user>\n"
        "open D:\\Work\\<user>\\notes.txt\n"
        '{"out": "USER PID\\n<user> 42", "prompt": "[32m<user>@box"}\n'
        "~/.kiro/crew/gateway.log\n"
        f"{kept}\n"
    ), out
    assert n == 6


def test_collapse_covers_the_windows_short_spelling_of_the_home(monkeypatch):
    """A home Windows also names by its 8.3 short form collapses in that form too.

    The native, forward-slash and JSON-escaped short paths keep their tail, the
    short login alone becomes ``<user>``, and another short name it is a prefix
    of stays.
    """
    back = "\\"
    users = "C:" + back + "Users" + back  # assembled so no literal home path is on a line
    base, short = users + "jonathansmith", users + "JONATH~1"
    monkeypatch.setattr(diagnostics.Path, "home", classmethod(lambda cls: Path(base)))
    monkeypatch.setattr(diagnostics, "config_dir", lambda: Path(base + back + ".kiro"))
    monkeypatch.setattr(diagnostics.diagnostics_host_name, "host_names", lambda: [])
    monkeypatch.setattr(
        diagnostics.platform_compat, "short_path_name", lambda p: short if p == base else ""
    )
    tail = back + "AppData" + back + "Local" + back + "Temp" + back + "x.log"
    spellings = [lambda t: t, lambda t: t.replace(back, "/"), lambda t: t.replace(back, back * 2)]

    out, _, n = diagnostics._scrub_bundle(
        "\n".join([*(f(short + tail) for f in spellings), "cwd JONATH~1 ok", "JONATH~10 too"])
    )

    assert out == "\n".join(
        [*(f("~" + tail) for f in spellings), "cwd <user> ok", "JONATH~10 too"]
    ), out
    assert n == 4


def test_bundle_replaces_the_login_after_a_doubly_serialized_escape(monkeypatch):
    """A login behind a ``\\\\n`` or ``\\\\t`` of twice-serialized output goes too.

    The doubled backslash in front of the escape is not read as a UNC path, and
    a sibling login behind the same escape still stays.
    """
    login = "nathansmith"
    home = "/" + "home" + "/" + login  # assembled so no literal home path is on a line
    monkeypatch.setattr(diagnostics.Path, "home", classmethod(lambda cls: Path(home)))
    monkeypatch.setattr(diagnostics, "config_dir", lambda: Path(home + "/.kiro/crew"))
    monkeypatch.setattr(diagnostics.diagnostics_host_name, "host_names", lambda: [])

    def note(name: str) -> str:
        inner = {"stdout": f"\n{name} 42", "err": f"\t{name}", "pid": f"\n{login}2"}
        return json.dumps({"text": json.dumps(inner)})

    out, _, n = diagnostics._scrub_bundle(note(login))

    assert out == note("<user>"), out
    assert n == 2


def test_bundle_replaces_a_login_that_a_serialized_escape_follows(monkeypatch):
    """A login on a line of its own in serialized output goes too.

    The ``\\`` of the escape after it is no path separator, so ``\\n<login>\\n``
    is not read as a Windows folder that the escape letter starts.
    """
    login = "nathansmith"
    home = "/" + "home" + "/" + login  # assembled so no literal home path is on a line
    monkeypatch.setattr(diagnostics.Path, "home", classmethod(lambda cls: Path(home)))
    monkeypatch.setattr(diagnostics, "config_dir", lambda: Path(home + "/.kiro/crew"))
    monkeypatch.setattr(diagnostics.diagnostics_host_name, "host_names", lambda: [])

    def note(name: str) -> str:
        return json.dumps({"stdout": f"alice\n{name}\ncarol\n", "err": f"x\t{name}\r\n"})

    out, _, n = diagnostics._scrub_bundle(note(login))

    assert out == note("<user>"), out
    assert n == 2
