"""Pre-flight: can systemd's own domain execute the binary we are about to
name in ``ExecStart``?

On an SELinux-enforcing host where kirocrew is installed under ``$HOME`` — the
default on Bazzite, Fedora Silverblue/Kinoite and every other atomic desktop —
a **system**-scope unit at ``/etc/systemd/system`` can never start. PID 1 runs
in the ``init_t`` domain, the binary carries a home label (``user_home_t``), and
the loaded policy does not grant ``init_t`` ``execute`` on that label. ``execve``
returns ``EACCES`` and systemd reports ``status=203/EXEC``, which it then repeats
until ``StartLimitBurst`` is exhausted (#7165).

**Why this needs a dedicated check instead of an ordinary file test.** ``203/EXEC``
has several causes that are indistinguishable in the unit's status output: the
path does not exist, it is not executable, its shebang interpreter is wrong, or
the file is *perfectly fine* and SELinux refused the execute. Only the last one
survives every check an installer would normally run — the policy grants
``getattr`` on a home-labelled file while denying ``execute``, so ``os.access(...,
X_OK)`` / ``test -x`` return **True** and the file looks correct right up to the
moment systemd tries to run it. A mode check therefore cannot answer this
question; the only thing that can is the policy itself.

So we ask the policy directly, through the kernel's ``compute_av`` interface
(``/sys/fs/selinux/access``). That is a pure query — it decides nothing, relabels
nothing, needs no root, and writes no state anywhere on the host.

**Mechanism-based, never distro-based** — the same rule :mod:`kiro_crew.service
.apparmor` states for its own gate, for the same reason: an ID check would miss
every derivative (Bazzite is a Fedora derivative of a derivative) and would
wrongly fire on a Fedora host that installed kirocrew system-wide, or one whose
operator has loaded a policy module granting the access. Nothing here matches a
distro name, a version, or even a *type* name. Every input is read from the
running kernel:

* enforcing state from ``/sys/fs/selinux/enforce``,
* the domain that will do the ``execve`` from ``/proc/1/attr/current`` (whatever
  PID 1 actually is on this host, not a hardcoded ``init_t``),
* the file's label from its ``security.selinux`` xattr,
* the verdict from the loaded policy.

**It fires only on a proven positive, and fails OPEN everywhere else.** SELinux
absent, permissive, an unreadable label, a kernel that refuses the query, a
policy whose source domain is *per-domain permissive* — every one of those
returns "not blocked" and the install proceeds exactly as it did before this
module existed. A pre-flight that guesses wrong in the refusing direction would
break installs that work today, which is strictly worse than the bug it prevents.

**No override knob, deliberately.** The check reads the live policy, so an
operator who loads a policy module granting the access flips it off by itself —
``compute_av`` starts answering ALLOW and the gate goes quiet with no flag to
remember. An escape hatch could only ever re-enable an install that provably
crash-loops.

**Coverage boundary, stated because it is reachable.** This answers exactly one
question: may PID 1's domain execute the file systemd itself ``execve``s at unit
start, and the interpreter its shebang names? It does NOT and cannot cover what
that file goes on to exec at RUNTIME. A ``KIROCREW_SERVICE_BIN`` override naming a
system-labelled wrapper that later runs a binary under ``$HOME`` therefore passes
this gate and still fails to serve — with the shell's exit 126, not ``203/EXEC``,
since the wrapper itself execs fine.

That hole cannot be closed soundly here. Following the delegation would mean
deciding what an arbitrary shell script executes, and the weaker version —
scanning a wrapper for path-shaped literals — would refuse installs over a path in
a comment or an untaken branch, because a literal appearing in a script is not
proof it is ever executed. Refusing on "cannot inspect" is the same violation from
the other side. So the boundary is left where soundness puts it, and
:func:`kiro_crew.service.linux.install` covers the residue at the point of failure
instead: when the unit is written and the first ``systemctl restart`` fails on an
enforcing host, its error names SELinux as a candidate and prints the same
user-scope remedy. Nothing is left silently unexplained, and no install that would
have worked is refused.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

SELINUX_FS = Path("/sys/fs/selinux")
_ENFORCE_PATH = SELINUX_FS / "enforce"
_ACCESS_PATH = SELINUX_FS / "access"
_CLASS_DIR = SELINUX_FS / "class"
# The domain that performs the execve for a system-scope unit: whatever PID 1
# is. Read rather than assumed — "init_t" is the Fedora/refpolicy answer, but
# the point of this module is to ask the host instead of encoding one policy's
# type names.
_SYSTEM_MANAGER_ATTR = Path("/proc/1/attr/current")

# libselinux's SELINUX_AVD_FLAGS_PERMISSIVE. Set in the compute_av reply when
# the SOURCE DOMAIN is marked permissive in policy, which means the kernel logs
# the denial and allows the access anyway. A denial from a permissive domain is
# therefore not a start failure, so it must not refuse an install.
_AVD_FLAG_PERMISSIVE = 0x0001

# Bytes of a shebang we are willing to read. A Linux kernel truncates the
# interpreter line at BINPRM_BUF_SIZE (256) anyway, so nothing beyond it can
# affect which interpreter actually runs.
_SHEBANG_LIMIT = 256


def is_enforcing() -> bool:
    """True only when the kernel is actively enforcing policy.

    In permissive mode the denial is logged and the ``execve`` succeeds, so the
    unit starts and there is nothing to warn about. An absent or unreadable
    ``enforce`` node (no SELinux, selinuxfs not mounted, a container) reads as
    not enforcing — the fail-open direction.
    """
    try:
        return _ENFORCE_PATH.read_text(encoding="ascii").strip() == "1"
    except OSError:
        return False


def _system_manager_context() -> str | None:
    """SELinux context of PID 1, or None when it cannot be read.

    This is the domain that will call ``execve`` on the unit's ``ExecStart`` for
    a system-scope unit, so it is the only correct source context for the query.
    """
    try:
        raw = _SYSTEM_MANAGER_ATTR.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    # The kernel NUL-terminates the attribute; a bare strip leaves it behind and
    # the policy lookup would then fail to parse the context.
    context = raw.strip().rstrip("\x00").strip()
    return context or None


def _file_context(path: str) -> str | None:
    """SELinux label of ``path``, following symlinks. None when unavailable.

    Symlinks are resolved because the kernel checks the label of the file it
    actually executes, and the shipped install path is a symlink chain
    (``~/.local/bin/kirocrew`` → ``~/.kiro/crew-venv/bin/kirocrew``). Reading the
    label of the *link* would answer the wrong question.
    """
    getxattr = getattr(os, "getxattr", None)  # Linux-only; absent elsewhere.
    if getxattr is None:
        return None
    try:
        raw = getxattr(os.path.realpath(path), "security.selinux")
    except (OSError, ValueError):
        return None
    try:
        context = raw.decode("ascii").strip().rstrip("\x00").strip()
    except UnicodeDecodeError:
        return None
    return context or None


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def _class_index(object_class: str) -> int | None:
    """Numeric class id the ``compute_av`` interface expects."""
    return _read_int(_CLASS_DIR / object_class / "index")


def _perm_bit(object_class: str, permission: str) -> int | None:
    """Access-vector bit for one permission of one class.

    selinuxfs exposes the 1-based BIT NUMBER, not the mask, so the value is
    shifted rather than used directly — reading it as a mask would test an
    unrelated permission (``execute`` is bit 15, i.e. mask ``0x4000``).
    """
    bit = _read_int(_CLASS_DIR / object_class / "perms" / permission)
    if bit is None or bit < 1 or bit > 32:
        return None
    return 1 << (bit - 1)


def _query_access(query: str) -> str | None:
    """Put one question to ``/sys/fs/selinux/access`` and return the raw reply.

    The write carries the question and the read returns the answer on the SAME
    descriptor — that is the interface's contract, not an accident, so the two
    cannot be split across separate opens. Nothing is relabelled, no policy is
    modified, and no privilege is required beyond the calling domain's own
    ``security { compute_av }``. Any failure returns None, which every caller
    treats as "no verdict".

    Separate from :func:`_compute_av` so the parsing can be exercised without a
    test having to monkeypatch ``os.open`` / ``os.read`` globally — patching
    those breaks pytest's own I/O, and a test that reaches for them is one edit
    away from writing to real selinuxfs.
    """
    try:
        fd = os.open(_ACCESS_PATH, os.O_RDWR)
    except OSError:
        return None
    try:
        os.write(fd, query.encode("ascii"))
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 4096).decode("ascii")
    except (OSError, UnicodeDecodeError, UnicodeEncodeError):
        return None
    finally:
        os.close(fd)


def _compute_av(
    source_context: str, target_context: str, object_class: str
) -> tuple[int, int] | None:
    """Ask the loaded policy for ``(allowed_vector, flags)``, or None.

    A read-only decision query: ``ffffffff`` requests the full access vector so
    one round trip answers every permission of the class.
    """
    class_id = _class_index(object_class)
    if class_id is None:
        return None
    reply = _query_access(f"{source_context} {target_context} {class_id} ffffffff")
    if reply is None:
        return None
    # "allowed decided auditallow auditdeny seqno flags", all hex but seqno.
    fields = reply.split()
    if len(fields) < 6:
        return None
    try:
        return int(fields[0], 16), int(fields[5], 16)
    except ValueError:
        return None


def _interpreter_of(path: str) -> str | None:
    """Interpreter named by ``path``'s shebang, or None if it is not a script.

    The installed entry point is a shebang script naming the venv interpreter,
    and that interpreter is under ``$HOME`` too. Checking only the script would
    pass a case that still fails at ``execve`` of the interpreter — a false
    negative that would let the doomed install proceed and make this gate look
    broken rather than absent.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(_SHEBANG_LIMIT)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    line = head[2:].split(b"\n", 1)[0]
    try:
        text = line.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    # `#!/usr/bin/env python3` names env as the executed file, which is what the
    # kernel checks; the first token is the right answer either way.
    candidate = text.split()[0]
    return candidate if candidate.startswith("/") else None


def _execute_denied(source_context: str, path: str) -> str | None:
    """Label of ``path`` when ``source_context`` provably may not execute it.

    None means "no proven denial" and covers every indeterminate case: no label,
    no policy answer, a permissive source domain, or an outright ALLOW.
    """
    label = _file_context(path)
    if label is None:
        return None
    execute_bit = _perm_bit("file", "execute")
    if execute_bit is None:
        return None
    verdict = _compute_av(source_context, label, "file")
    if verdict is None:
        return None
    allowed, flags = verdict
    if flags & _AVD_FLAG_PERMISSIVE:
        # Denials from a permissive domain are logged, not enforced, so the
        # execve still succeeds and the unit starts.
        return None
    if allowed & execute_bit:
        return None
    return label


def blocks_system_unit(exec_path: str) -> tuple[bool, str]:
    """Whether a system unit running ``exec_path`` provably cannot start.

    Returns ``(blocked, reason)``. ``reason`` is always populated so a caller can
    report why the gate stayed quiet as readily as why it fired.

    The three conditions are independent and all must hold, each read from the
    running kernel rather than inferred: policy is being enforced, PID 1's domain
    is known, and that domain is denied ``execute`` on the file systemd would run
    (the resolved binary, or the interpreter its shebang names).
    """
    if not is_enforcing():
        return False, "SELinux is not enforcing on this host"
    source_context = _system_manager_context()
    if source_context is None:
        return False, f"could not read the system manager's domain from {_SYSTEM_MANAGER_ATTR}"

    # The binary first, then the interpreter its shebang names: either one being
    # denied is enough to guarantee 203/EXEC, and naming the right file makes the
    # difference between an actionable message and a puzzle.
    resolved = os.path.realpath(exec_path)
    candidates = [resolved]
    interpreter = _interpreter_of(resolved)
    if interpreter:
        candidates.append(os.path.realpath(interpreter))

    for candidate in candidates:
        label = _execute_denied(source_context, candidate)
        if label is not None:
            return True, (
                f"SELinux is enforcing and its policy does not allow "
                f"{source_context} (systemd, PID 1) to execute {candidate}, "
                f"which is labelled {label}"
            )
    return (
        False,
        f"SELinux policy allows {source_context} to execute " f"{' and '.join(candidates)}",
    )
