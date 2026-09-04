"""Install advice for the optional dependency extras.

``kirocrew`` is NOT published on any index, so the obvious advice --
``pip install "kirocrew[feishu]"`` -- is not merely unhelpful, it cannot resolve
at all: pip fails with *No matching distribution found for kirocrew* however the
user installed Kiro Crew. On a hypothetical index it would be worse than an
error, because installing ``kirocrew[extra]`` REPLACES an editable/source
install with a released wheel -- the command reports success while swapping the
gateway out from under the user.

So the advice names the extra's own distributions with the pins declared in
``setup.cfg``. Those resolve from PyPI for every install layout, and they add
exactly the missing dependency without touching Kiro Crew itself.

:data:`REQUIREMENTS` is that list, transcribed once here with self-referential
extras expanded, and pinned against ``setup.cfg`` by ``test/test_extras.py`` so
the two cannot drift. Reading it from installed distribution metadata instead
was tried and dropped: the metadata is generated from this same ``setup.cfg``,
so it can never carry a NEWER pin than this map -- only an older one, from a
stale editable install's ``.dist-info``.
"""

from __future__ import annotations

import os
import shlex
import sys

#: Distributions each optional extra installs, keyed by extra name. Mirrors
#: ``[options.extras_require]`` in ``setup.cfg``, with ``voice``'s
#: self-reference to ``voice-aws`` already expanded. ``dev`` is deliberately
#: absent: a running gateway never advises installing the test tooling.
REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "otlp": ("opentelemetry-exporter-otlp-proto-http==1.44.0",),
    "perf": ("py-spy>=0.3,<1",),
    "teams": ("PyJWT[crypto]==2.13.0",),
    "whatsapp": ("neonize==0.4.3.post0",),
    "feishu": ("lark-oapi>=1.4,<2",),
    "voice-aws": ("boto3>=1.34,<2", "amazon-transcribe>=0.6,<1"),
    "voice": ("boto3>=1.34,<2", "amazon-transcribe>=0.6,<1", "pywhispercpp>=1.5,<2"),
}


def quote_spec(spec: str) -> str:
    r"""*spec* quoted so BOTH shells on this platform pass it to pip intact.

    A requirement specifier is not a plain word: a bounded pin holds ``<`` and
    ``>``, which are redirection operators. Unquoted,
    ``pip install lark-oapi>=1.4,<2`` makes the shell create a file called
    ``=1.4,`` and read one called ``2`` -- pip never sees the bound.

    On Windows the shell is unknowable here (cmd OR PowerShell), so the form has
    to be right in both, and the POSIX answer is actively wrong: cmd does not
    treat ``'`` as a quote, so ``'lark-oapi>=1.4,<2'`` still redirects on the
    ``<``. DOUBLE quotes are the one form both accept -- cmd and PowerShell each
    strip them and hand pip the literal. They are safe for a specifier
    specifically because a pin contains no ``$``, backtick or ``"`` for
    PowerShell to expand; ``test_specifiers_hold_no_powershell_metacharacters``
    pins that precondition, so a future pin cannot silently break it.
    """
    if os.name == "nt":
        return f'"{spec}"'
    return shlex.quote(spec)


def requirements_for_extra(extra: str) -> tuple[str, ...]:
    """Requirement strings that install *extra*, without naming this project.

    Empty for an extra this build does not declare, which callers render as
    "no install command" rather than as a broken one.
    """
    return REQUIREMENTS.get(extra.strip().lower(), ())


def install_hint(extra: str) -> str:
    """Short ``pip install ...`` string for a log line or CLI message.

    No interpreter prefix: these land in log records and terminal output where
    the surrounding text already says which environment is meant, and an
    absolute path would bury the package names. The dashboard, where the user
    copies the command blind, uses :func:`pip_install_command` instead.

    Because there is no prefix, this string reads as an ordinary command in
    whatever shell the user is in -- so the specifiers are quoted per platform
    (see :func:`quote_spec`) rather than POSIX-only.
    """
    reqs = requirements_for_extra(extra)
    if not reqs:
        return ""
    return "pip install " + " ".join(quote_spec(r) for r in reqs)


def pip_install_command(extra: str) -> str:
    r"""The command that installs *extra* into THIS python, ready to paste.

    The interpreter is spelled out rather than left as a bare ``pip`` because
    "which python" is the failure this string exists to prevent: a gateway run
    from one venv while the user installs into another leaves the feature
    missing, and nothing in the UI says the install landed somewhere else. The
    dependency is imported by the gateway process itself, so a system python or
    a ``--user`` install is not importable here.

    On Windows the user's shell is unknowable here (they may paste this into
    PowerShell OR cmd), so the form must be SILENT-CORRUPTION-FREE in both, and
    PowerShell is the harder shell: a double-quoted string still expands
    ``$name`` and honours backtick escapes, and so does a bare unquoted token --
    both are legal path characters, so either form silently rewrites an
    interpreter under e.g. ``C:\tools\$python\...`` into a path that does not
    exist. Single quotes are PowerShell's LITERAL form (no expansion, no
    escapes, spaces included), with ``&`` invoking the quoted path, so the
    interpreter reaches pip byte-for-byte -- including the all-users
    ``C:\Program Files\...`` layout an unquoted form cannot express. cmd performs
    no ``$`` or backtick processing at all and rejects the leading ``&`` loudly
    ("... was unexpected"), so a cmd user gets a clear error to re-quote for,
    never a corrupted install. A literal single quote in the path is escaped by
    doubling, PowerShell's own rule.

    Returns ``""`` for an extra this build does not declare, so a caller never
    shows a copy button for a command that installs nothing.
    """
    reqs = requirements_for_extra(extra)
    if not reqs:
        return ""
    specs = " ".join(quote_spec(r) for r in reqs)
    if os.name == "nt":
        exe = sys.executable.replace("'", "''")
        return f"& '{exe}' -m pip install {specs}"
    return f"{shlex.quote(sys.executable)} -m pip install {specs}"
