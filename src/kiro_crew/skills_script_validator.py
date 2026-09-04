"""Static safety validation for generated skill scripts.

Runs on every script-bearing candidate — including the auto-approve path — so a
dangerous script can never reach the live skill set unscanned (mirrors the way
Hermes' ``guard_agent_created`` scans independently of the approval gate).

This is a deterministic, LLM-free gate. It is intentionally conservative: it
rejects destructive commands, credential/sensitive-path access, network egress,
oversized files, non-Python scripts, and syntax errors. A candidate that fails
here is not staged (or, on the auto-approve path, not published) — the advisory
Haiku review is a separate, softer signal layered on top of this hard gate.
"""

from __future__ import annotations

import ast
import re
from typing import List, Tuple

from kiro_crew.security import _SENSITIVE_HOME_DIRS

# Hard cap per script. Larger => the generator went off-task.
MAX_SCRIPT_BYTES = 4096

# Destructive / dangerous command patterns (case-insensitive).
_DESTRUCTIVE = [
    (re.compile(r"\brm\s+-rf?\b", re.I), "destructive: rm -rf"),
    (re.compile(r"\brmdir\b\s+/", re.I), "destructive: rmdir on root path"),
    (re.compile(r"\bmkfs\b", re.I), "destructive: mkfs"),
    (re.compile(r"\bdd\s+if=", re.I), "destructive: dd if="),
    (re.compile(r">\s*/dev/sd", re.I), "destructive: write to block device"),
    (re.compile(r"\bDROP\s+(TABLE|DATABASE)\b", re.I), "destructive: SQL DROP"),
    (re.compile(r"\bTRUNCATE\s+TABLE\b", re.I), "destructive: SQL TRUNCATE"),
    (re.compile(r"shutil\.rmtree", re.I), "destructive: shutil.rmtree"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:", re.I), "destructive: fork bomb"),
]

# Credential / sensitive path access. The path set is derived from the
# canonical ``security._SENSITIVE_HOME_DIRS`` (the same list backing
# ``is_sensitive_path()``), so the validator stays complete and in-sync with the
# shared file-read gate instead of maintaining a partial hand-rolled list — it
# covers ~/.aws, ~/.ssh, ~/.gnupg, ~/.docker/config.json, ~/.npmrc, ~/.pypirc,
# ~/.netrc, ~/.git-credentials, the SSO/kiro-cli auth stores, and KiroCrew's own
# governance trust-root files. The trailing ``(?!\w)`` keeps ``.env`` from
# matching ``os.environ`` while still catching ``.env`` / ``.env.local``.
_SENSITIVE_PATH_RE = re.compile(
    "(?:"
    + "|".join(re.escape(d) for d in sorted(_SENSITIVE_HOME_DIRS, key=len, reverse=True))
    + r")(?!\w)",
    re.I,
)
_SENSITIVE = [
    (re.compile(r"\b169\.254\.169\.254\b"), "credential access: cloud metadata IP"),
    (re.compile(r"\bid_rsa\b", re.I), "credential access: id_rsa"),
    (re.compile(r"os\.environ\[[\"'][A-Z_]*(TOKEN|SECRET|KEY|PASSWORD)", re.I),
     "credential access: reads a secret env var"),
    (re.compile(r"os\.(?:getenv|environ\.get)\(\s*[\"'][A-Z_]*(TOKEN|SECRET|KEY|PASSWORD)", re.I),
     "credential access: reads a secret env var"),
    (_SENSITIVE_PATH_RE, "credential access: sensitive path"),
]

# Network egress — flagged for review (we can't safely allowlist hosts here).
_NETWORK = [
    (re.compile(r"\brequests\.(get|post|put|delete|patch|head)\b", re.I), "network egress: requests"),
    (re.compile(r"\burllib\.request\b|urlopen\(", re.I), "network egress: urllib"),
    (re.compile(r"\bsocket\.(socket|create_connection)\b", re.I), "network egress: socket"),
    (re.compile(r"\bhttpx?\.(get|post|Client|AsyncClient)\b", re.I), "network egress: httpx"),
    (re.compile(r"\b(curl|wget)\s+https?://", re.I), "network egress: curl/wget"),
]


# Dangerous builtins that enable arbitrary code / import at runtime, defeating
# a static denylist (e.g. ``__import__("os").remove(...)``).
_BANNED_CALL_NAMES = {"eval", "exec", "compile", "__import__"}
# Dangerous ``module.attr(...)`` calls (destructive fs / process exec / dynamic
# import), keyed by attribute name — matched regardless of the module alias.
_BANNED_ATTR_CALLS = {
    "system", "popen",                        # os.system / os.popen
    "remove", "unlink", "rmdir", "removedirs",  # os.* / Path.* destructive fs
    "rmtree",                                 # shutil.rmtree
    "Popen", "run", "call", "check_call", "check_output",  # subprocess.*
    "import_module",                          # importlib.import_module
    # Process replacement and creation that lives on ``os``. The module itself
    # cannot be banned — a skill legitimately needs os.path/os.environ — so the
    # specific calls are named instead: os.exec* replaces this process with a
    # program of the script's choosing, os.spawn*/posix_spawn start one
    # alongside, and os.fork/forkpty duplicate the interpreter. Each reaches
    # arbitrary execution without ever naming ``subprocess``. These names are
    # matched on the attribute alone (see ``_ast_findings``), which is why only
    # os-specific spellings belong here — a common name like ``load`` would
    # collide with ``json.load``.
    "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp",
    "spawnvpe", "posix_spawn", "posix_spawnp",
    "fork", "forkpty", "openpty",
    # os.startfile hands a path to the Windows shell, which launches it with
    # whatever handler is registered — an .exe runs, and a document runs its
    # application. It is the Windows-only sibling of the exec family above and
    # exists on no other platform, so nothing benign is lost by naming it.
    "startfile",
    # asyncio egress primitives — high-level stream/server openers and their
    # loop-level equivalents. Banned by attribute name (alias-proof) so a script
    # can't reach a remote host via ``asyncio.open_connection()`` etc. while the
    # module itself stays importable for benign async control flow (run/sleep/
    # gather). ``create_server``/``create_connection`` cover the loop-level API.
    "open_connection", "open_unix_connection",
    "start_server", "start_unix_server",
    "create_connection", "create_server",
}

# Import roots that enable dynamic code / process exec (alias-proof: matched on
# the imported module, not on the call site).
#
# Banning the root rather than the call is what keeps these precise. The
# call-site check matches an attribute name against every module, so putting
# ``load``/``loads`` there would reject ``json.load`` as readily as
# ``pickle.load``; banning the ``pickle`` import instead reaches the same
# payload and leaves the safe parsers alone.
#
#   pty            allocates a terminal and runs a program in it — an
#                  interactive shell, which a denylist keyed on ``subprocess``
#                  never sees
#   pickle,        unpickling calls ``__reduce__`` on the incoming bytes, so
#   marshal        loading attacker-controlled data is execution, not parsing
#   multiprocessing  Process(target=...).start() runs a callable in a new
#                  interpreter, so the payload need not be a string command
#   runpy          executes a module or a file as ``__main__``
#   code           evaluates source in a live interpreter — exec by another name
#   builtins       every name this module denies as a bare call — eval, exec,
#                  compile, __import__, getattr, vars — is reachable again as
#                  ``builtins.eval(...)``, which the bare-name check does not
#                  see. The builtins are available without the import, so a
#                  script that reaches for the module is asking for the
#                  qualified spelling and nothing else.
_DANGEROUS_IMPORT_ROOTS = {
    "subprocess", "ctypes", "importlib",
    "pty", "pickle", "marshal", "multiprocessing", "runpy", "code",
    "builtins",
}
# Module roots whose banned attributes are dangerous even when merely referenced
# (assigned/aliased) rather than called directly (``f = os.remove``).
_DANGEROUS_ATTR_ROOTS = {"os", "shutil", "subprocess", "importlib", "ctypes"}
# Network/egress library roots. Banned outright in generated scripts: the skill
# contract forbids calling unknown network hosts, and matching on the import
# (not the call) defeats alias bypasses like ``import requests as r; r.get(...)``
# that the regex denylist and call-site checks miss.
_NETWORK_IMPORT_ROOTS = {
    "requests", "httpx", "aiohttp", "urllib", "urllib3", "http",
    "socket", "ftplib", "smtplib", "telnetlib", "poplib", "imaplib",
    # webbrowser.open(url) is a covert egress channel: a secret embedded in the
    # launched URL leaves via the browser, bypassing the HTTP-client denylist.
    "webbrowser",
}
# Builtins that hand back a module's namespace, so a banned attribute can be
# resolved from a string at runtime: ``getattr(os, "execv")``,
# ``vars(os)["execv"]``. The attribute name is a string constant this pass does
# not evaluate, so the module argument is what gets flagged.
_NAMESPACE_LOOKUP_NAMES = {"getattr", "vars"}

# Attributes that expose a module's namespace as a mapping, reachable with a
# subscript: ``os.__dict__["execv"]``. Same reasoning as above — deny the handle,
# not the key.
_NAMESPACE_ATTRS = {"__dict__", "__getattribute__", "__getattr__"}

# Builtin types whose descriptor methods can retrieve arbitrary attributes from
# any object — ``object.__getattribute__(os, "__dict__")["execv"]`` bypasses the
# namespace guard because the *base* is ``object``, not a dangerous module.  The
# first *argument* is the dangerous module in this pattern.  Deny these calls
# when the first positional argument resolves to a dangerous module root.
_BUILTIN_DESCRIPTOR_BASES = {"object", "type", "super"}
_DESCRIPTOR_METHODS = {"__getattribute__", "__getattr__"}

# Dangerous callables that must not be pulled in via ``from <mod> import <name>``
# (which would bind a bare name the call-site checks miss, e.g.
# ``from os import remove; remove(x)``). Checked on the ORIGINAL imported name,
# so ``from os import remove as rm`` is caught too.
_DANGEROUS_IMPORTED_NAMES = _BANNED_CALL_NAMES | _BANNED_ATTR_CALLS


def _ast_findings(content: str) -> List[str]:
    """Walk the script AST for dangerous constructs a regex denylist misses.

    Catches dynamic exec/import (``eval``/``exec``/``compile``/``__import__``),
    destructive filesystem calls (``os.remove``/``unlink``/``rmdir``/
    ``shutil.rmtree``/``Path.unlink`` ...), process execution (``os.system``/
    ``subprocess.*``), and dynamic import (``importlib.import_module``) — so an
    obfuscated payload like ``__import__("os").remove(x)`` is rejected.
    """
    findings: List[str] = []
    try:
        tree = ast.parse(content or "")
    except SyntaxError:
        return findings  # syntax error is reported separately by the caller
    # Pre-pass: map each bound name to its imported module root, so aliased
    # access (``import os as x; x.remove``) resolves to the real module.
    alias_map: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                alias_map[a.asname or root] = root
    # A plain rebinding is an alias too, and the checks below all resolve their
    # operand through this map — so without it, one assignment hid the operand:
    #
    #     o = object; getattr(o, "__getattribute__")(os, "__dict__")[...]
    #     m = os;     object.__getattribute__(m, "__dict__")[...]
    #
    # ``o`` / ``m`` are bare Names, so the shape checks matched, but the map
    # resolved them to themselves and neither is a dangerous root or a builtin
    # base. Follow ``NAME = NAME`` chains to a fixpoint (bounded by the number of
    # assignments, so it terminates) and seed the builtin bases, which are not
    # imports and so were never in the map at all. Order-independent: a use that
    # textually precedes its assignment still resolves, which is the safe
    # direction for a deny check.
    for builtin_base in _BUILTIN_DESCRIPTOR_BASES:
        alias_map.setdefault(builtin_base, builtin_base)
    for _ in range(len(_BUILTIN_DESCRIPTOR_BASES) + 8):
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Name):
                continue
            resolved = alias_map.get(node.value.id)
            if resolved is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and alias_map.get(target.id) != resolved:
                    alias_map[target.id] = resolved
                    changed = True
        if not changed:
            break
    # A dangerous builtin bound to another name defeats every check keyed on the
    # call site: ``lookup = getattr; lookup(os, "execv")`` calls through `lookup`,
    # so `fn.id` never matches. Rather than chase the binding, reject the bare
    # name wherever it is *loaded* without being called. Scoped to a Load
    # context, so assigning TO one of these names is unaffected, and the call
    # forms below still report the more specific finding.
    _rebindable = _BANNED_CALL_NAMES | _NAMESPACE_LOOKUP_NAMES
    called_names = {
        n.func for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and node.id in _rebindable
            and isinstance(node.ctx, ast.Load)
            and node not in called_names
        ):
            findings.append(f"dangerous builtin rebound: {node.id}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in _BANNED_CALL_NAMES:
                findings.append(f"dynamic exec/import: {fn.id}()")
            elif isinstance(fn, ast.Name) and fn.id in _NAMESPACE_LOOKUP_NAMES and node.args:
                # ``getattr(os, "remove")`` and ``vars(os)["remove"]`` both
                # resolve a banned attribute at runtime, so the static attribute
                # check never sees the name. Flag on the module argument alone —
                # the attribute is a string this pass will not evaluate.
                a0 = node.args[0]
                if isinstance(a0, ast.Starred):
                    # ``getattr(*pair)`` hides both operands, so neither the
                    # module check nor the builtin-base check can run, and the
                    # result is then called: ``getattr(*a)(os, "__dict__")[...]``.
                    # A namespace lookup with unpacked arguments has no benign
                    # use here — ``print(*args)`` is unaffected because print is
                    # not one of these two functions.
                    findings.append(f"namespace lookup with unpacked arguments: {fn.id}(*...)")
                root = alias_map.get(a0.id, a0.id) if isinstance(a0, ast.Name) else ""
                if root in _DANGEROUS_ATTR_ROOTS:
                    findings.append(f"dynamic attribute access: {fn.id}({root}, ...)")
                elif root in _BUILTIN_DESCRIPTOR_BASES:
                    # The descriptor base reached INDIRECTLY. The check below
                    # covers ``object.__getattribute__(os, ...)``, where the base
                    # is an Attribute node it can read; routing the same lookup
                    # through this function hides it, because the outer call's
                    # ``func`` is then a Call (or a Subscript, or a plain Name
                    # after a bind) and matches none of these branches:
                    #
                    #     getattr(object, "__getattribute__")(os, "__dict__")[...]
                    #     vars(object)["__getattribute__"](os, "__dict__")[...]
                    #     f = getattr(object, "__getattribute__"); f(os, ...)
                    #
                    # Flagging the LOOKUP rather than its application is what
                    # makes this hold: whatever the retrieved descriptor is later
                    # applied to, the finding has already been recorded, so no
                    # downstream spelling can walk it back. ``getattr(super(), x)``
                    # is unaffected — its first argument is a Call, not a Name.
                    findings.append(
                        f"descriptor lookup on a builtin base: {fn.id}({root})"
                    )
            elif isinstance(fn, ast.Attribute) and fn.attr in _BANNED_ATTR_CALLS:
                findings.append(f"dangerous call: .{fn.attr}()")
            elif (
                isinstance(fn, ast.Attribute)
                and fn.attr in _DESCRIPTOR_METHODS
                and node.args
            ):
                # ``object.__getattribute__(os, "__dict__")["execv"]`` bypasses
                # the namespace guard: the base is ``object``, not a dangerous
                # module, so the attribute check on _NAMESPACE_ATTRS never fires.
                # Deny when the first positional arg resolves to a dangerous root.
                #
                # The BASE is deliberately unconstrained. Requiring it to be a
                # Name in _BUILTIN_DESCRIPTOR_BASES left the same call reachable
                # by respelling the base, which is free to the author:
                #
                #     type(os).__getattribute__(os, "__dict__")[...]      a Call
                #     os.__class__.__getattribute__(os, "__dict__")[...]  an Attribute
                #
                # What makes the call dangerous is the TARGET, so that is what is
                # tested. Keeping the first-argument condition is what preserves
                # the benign case: ``object.__getattribute__(c, "x")`` on an
                # ordinary object, and on a non-dangerous module, still pass.
                a0 = node.args[0]
                fn_base = fn.value
                base_desc = fn_base.id if isinstance(fn_base, ast.Name) else "<expr>"
                if isinstance(a0, ast.Name):
                    if alias_map.get(a0.id, a0.id) in _DANGEROUS_ATTR_ROOTS:
                        findings.append(
                            f"builtin descriptor bypass: {base_desc}.{fn.attr}({a0.id}, ...)"
                        )
                else:
                    # The target is not a name this pass can resolve — a starred
                    # argument, a conditional, a call. It CANNOT be cleared, and
                    # the whole point of the check is that the target decides
                    # whether the call is dangerous, so fail closed rather than
                    # let an unreadable operand through:
                    #
                    #     object.__getattribute__(*[os, "__dict__"])[...]
                    #     object.__getattribute__(os if c else os, "__dict__")[...]
                    #
                    # Narrow by construction: reaching a descriptor method
                    # explicitly is already exotic — ordinary attribute access is
                    # ``x.y`` — so this refuses a shape with no benign use, and
                    # the resolvable benign case (a plain non-dangerous name) is
                    # still allowed by the branch above.
                    findings.append(
                        f"descriptor call on an unresolvable target: "
                        f"{base_desc}.{fn.attr}(...)"
                    )
        elif isinstance(node, ast.Attribute) and node.attr in _BANNED_ATTR_CALLS:
            # Catch a dangerous callable *referenced* (not just called) off a
            # dangerous module — e.g. ``f = os.remove; f(x)`` or, via an alias,
            # ``import os as x; f = x.remove``. Resolve the base through the
            # alias map; scoped to known-dangerous roots so benign attributes
            # (``asyncio.run``, ``app.run``) aren't flagged.
            base = node.value
            if isinstance(base, ast.Name) and alias_map.get(base.id, base.id) in _DANGEROUS_ATTR_ROOTS:
                findings.append(f"dangerous attribute: {base.id}.{node.attr}")
        elif isinstance(node, ast.Attribute) and node.attr in _NAMESPACE_ATTRS:
            # ``os.__dict__["execv"]`` reaches the same callable through a
            # subscript, which the attribute check above cannot see: the name
            # lives in a string constant, not in the AST as an attribute. Deny
            # the namespace handle itself rather than trying to read the key.
            #
            # For ``__dict__`` a builtin descriptor base counts as dangerous for
            # the same reason it does at the lookup functions above:
            # ``object.__dict__["__getattribute__"]`` retrieves the descriptor
            # that then reads a dangerous module's namespace. Widened for that
            # attribute ONLY — extending it to the descriptor methods would flag
            # ``object.__getattribute__(c, "x")`` on a benign target, which the
            # call branch above deliberately allows. Bases outside both sets stay
            # allowed either way, so ``self.__dict__`` is unaffected.
            base = node.value
            dangerous_bases = _DANGEROUS_ATTR_ROOTS
            if node.attr == "__dict__":
                dangerous_bases = _DANGEROUS_ATTR_ROOTS | _BUILTIN_DESCRIPTOR_BASES
            if isinstance(base, ast.Name) and alias_map.get(base.id, base.id) in dangerous_bases:
                findings.append(f"dynamic attribute access: {base.id}.{node.attr}[...]")
        elif isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root in _DANGEROUS_IMPORT_ROOTS:
                    findings.append(f"dangerous import: {a.name}")
                elif root in _NETWORK_IMPORT_ROOTS:
                    findings.append(f"network egress import: {a.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _DANGEROUS_IMPORT_ROOTS:
                findings.append(f"dangerous import-from: {node.module}")
            elif root in _NETWORK_IMPORT_ROOTS:
                findings.append(f"network egress import-from: {node.module}")
            for a in node.names:
                if a.name in _DANGEROUS_IMPORTED_NAMES:
                    findings.append(f"dangerous import-from: {node.module}.{a.name}")
    # De-dupe while preserving order.
    seen: set[str] = set()
    out: List[str] = []
    for f in findings:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def validate_skill_script(filename: str, content: str) -> Tuple[bool, List[str]]:
    """Return ``(ok, findings)`` for a single generated script.

    ``ok`` is True only when there are zero findings. ``findings`` is a list of
    short human-readable reasons, suitable for surfacing in the pending review.
    """
    findings: List[str] = []
    fn = (filename or "").strip()

    if not fn.endswith(".py"):
        findings.append(f"unsupported language: only Python (.py) allowed, got {fn!r}")

    encoded = (content or "").encode("utf-8")
    if len(encoded) > MAX_SCRIPT_BYTES:
        findings.append(f"too large: {len(encoded)} bytes > {MAX_SCRIPT_BYTES} cap")

    for patterns in (_DESTRUCTIVE, _SENSITIVE, _NETWORK):
        for rx, label in patterns:
            if rx.search(content or ""):
                findings.append(label)

    # Syntax check (only meaningful for Python).
    if fn.endswith(".py") and content:
        try:
            compile(content, fn or "<script>", "exec")
        except SyntaxError as exc:
            findings.append(f"syntax error: {exc.msg} (line {exc.lineno})")
        else:
            # AST policy — catches obfuscated payloads the regex denylist misses
            # (dynamic exec/import, destructive fs, process exec).
            findings.extend(_ast_findings(content))

    return (not findings), findings


def validate_scripts(scripts: List[dict]) -> Tuple[bool, dict]:
    """Validate a list of ``{filename, content}`` scripts.

    Returns ``(all_ok, {filename: findings})`` — ``all_ok`` is True iff every
    script passed. Non-dict entries are reported as invalid.
    """
    report: dict = {}
    all_ok = True
    for s in scripts or []:
        if not isinstance(s, dict):
            all_ok = False
            report["<invalid>"] = ["not a script object"]
            continue
        fn = str(s.get("filename", "")).strip() or "<unnamed>"
        ok, findings = validate_skill_script(fn, str(s.get("content", "")))
        if not ok:
            all_ok = False
            report[fn] = findings
    return all_ok, report
