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
_DANGEROUS_IMPORT_ROOTS = {"subprocess", "ctypes", "importlib"}
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
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in _BANNED_CALL_NAMES:
                findings.append(f"dynamic exec/import: {fn.id}()")
            elif isinstance(fn, ast.Name) and fn.id == "getattr" and node.args:
                # ``getattr(os, "remove")`` (or an alias) dynamically resolves a
                # banned attribute, bypassing the static attribute check.
                a0 = node.args[0]
                if isinstance(a0, ast.Name) and alias_map.get(a0.id, a0.id) in _DANGEROUS_ATTR_ROOTS:
                    findings.append(f"dynamic attribute access: getattr({a0.id}, ...)")
            elif isinstance(fn, ast.Attribute) and fn.attr in _BANNED_ATTR_CALLS:
                findings.append(f"dangerous call: .{fn.attr}()")
        elif isinstance(node, ast.Attribute) and node.attr in _BANNED_ATTR_CALLS:
            # Catch a dangerous callable *referenced* (not just called) off a
            # dangerous module — e.g. ``f = os.remove; f(x)`` or, via an alias,
            # ``import os as x; f = x.remove``. Resolve the base through the
            # alias map; scoped to known-dangerous roots so benign attributes
            # (``asyncio.run``, ``app.run``) aren't flagged.
            base = node.value
            if isinstance(base, ast.Name) and alias_map.get(base.id, base.id) in _DANGEROUS_ATTR_ROOTS:
                findings.append(f"dangerous attribute: {base.id}.{node.attr}")
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
