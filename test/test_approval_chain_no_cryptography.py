"""The tool-approval hook's import chain must not depend on the `cryptography`
native wheel.

Background
----------
``hooks.on_tool_call`` -- the function every ACP tool call passes through for
its approve/deny verdict -- lazily imports ``kiro_crew.slack.gateway`` for
``_is_read_only_tool``. That module imports ``kiro_crew.channels``, which
imports every channel gateway, and the WeCom and Weixin gateways reach their
``media`` modules, which used to import ``cryptography.hazmat`` at module top
for their AES decryptors. So a host whose ``cryptography`` wheel does not load
(a platform-mismatched build: the AL2 x86_64 wheel on an Apple-silicon Mac)
made EVERY tool approval raise ImportError -- not just WeCom media downloads.

The fix moved those imports inside the two decrypt functions. These tests pin
the property in a subprocess with ``cryptography`` made unimportable, so a
future top-level import anywhere on the chain fails here rather than in a
security-path traceback on someone's machine.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

# Makes `import cryptography` (and any submodule) raise ImportError, the way a
# missing or unloadable wheel does. A meta_path finder rather than a sys.modules
# None entry, so submodule imports fail too.
_BLOCK_CRYPTOGRAPHY = textwrap.dedent("""
    import importlib.abc, sys

    class _Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == "cryptography" or name.startswith("cryptography."):
                raise ImportError(f"blocked for this test: {name}")
            return None

    sys.meta_path.insert(0, _Block())
    """)


def _run(snippet: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        # -B: the child imports repo modules and must not leave __pycache__ beside them.
        [sys.executable, "-B", "-c", _BLOCK_CRYPTOGRAPHY + textwrap.dedent(snippet)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_the_approval_hook_import_chain_loads_without_cryptography() -> None:
    proc = _run("""
        # The exact import hooks.on_tool_call performs, then the chain it drags in.
        from kiro_crew.slack.gateway import _is_read_only_tool
        import kiro_crew.channels
        import kiro_crew.wecom.media
        import kiro_crew.weixin.media
        import kiro_crew.secrets.vault
        assert _is_read_only_tool("Read") is True
        print("chain ok")
        """)
    assert proc.returncode == 0, proc.stderr
    assert "chain ok" in proc.stdout


def test_only_decrypting_media_needs_cryptography() -> None:
    # The dependency did not disappear, it moved to the one place that uses it:
    # decrypting still fails clearly when the wheel is unavailable.
    proc = _run("""
        from kiro_crew.wecom import media as wecom_media
        from kiro_crew.weixin import media as weixin_media
        from kiro_crew.secrets.vault import SecretVault
        import tempfile, pathlib
        vault = SecretVault.__new__(SecretVault)
        cases = (
            (lambda: wecom_media.decrypt_media(b"x" * 32, b"k" * 32)),
            (lambda: weixin_media.decrypt_aes_ecb(b"x" * 32, b"k" * 16)),
            (lambda: vault._decrypt_entry("n", {"nonce": "00" * 12, "ct": "00" * 16}, b"k" * 32)),
        )
        for fn in cases:
            try:
                fn()
            except ImportError as exc:
                assert "cryptography" in str(exc), exc
            else:
                raise SystemExit("decrypt ran without cryptography?!")
        print("decrypt needs it")
        """)
    assert proc.returncode == 0, proc.stderr
    assert "decrypt needs it" in proc.stdout


def test_no_module_on_the_channel_chain_imports_cryptography_at_top_level() -> None:
    """Static complement to the subprocess test: the chain's modules must not
    grow a top-level `cryptography` import back. Scoped to what the approval
    hook reaches -- the channel tree AND `secrets/` (slack.gateway -> autonudge
    -> irq -> cron_script -> secrets.vault was the second way in). auth/store.py
    is not on the chain and keeps its top-level import."""
    import ast
    from pathlib import Path

    import kiro_crew

    root = Path(kiro_crew.__file__).parent
    offenders = []
    for sub in ("wecom", "weixin", "slack", "secrets", "channels.py"):
        paths = [root / sub] if sub.endswith(".py") else sorted((root / sub).rglob("*.py"))
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:  # module top level only
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                if any(n == "cryptography" or n.startswith("cryptography.") for n in names):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == [], f"top-level cryptography import on the approval chain: {offenders}"
