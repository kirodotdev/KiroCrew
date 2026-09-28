"""R30 regression tests (round-30 Codex findings on 3b60307).

F1: staging must reject hardlinks on the OPENED inode (fstat after O_NOFOLLOW
    open), not only via a racy lstat-then-open-by-name sequence.
F2: reaper.yaml must supply ACCOUNT_ID to the Lambda env — the reaper role has
    no sts:GetCallerIdentity, so the STS fallback fails and expired
    engine-arch deployments would leak forever.
"""
import os
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = REPO / "src" / "kiro_crew" / "deploy" / "skills" / "artifact-deploy" / "templates"
HANDLERS = REPO / "src" / "kiro_crew" / "deploy" / "handlers.py"


class TestF1NolinkRead:
    def test_helper_rejects_hardlinked_inode(self, tmp_path, monkeypatch):
        from kiro_crew.hooks import safe_read_file_bytes_nolink
        target = tmp_path / "secret.txt"
        target.write_text("data")
        link = tmp_path / "link.txt"
        os.link(target, link)  # nlink == 2 on both
        assert safe_read_file_bytes_nolink(str(link)) is None
        assert safe_read_file_bytes_nolink(str(target)) is None

    def test_helper_reads_regular_file(self, tmp_path):
        from kiro_crew.hooks import safe_read_file_bytes_nolink
        f = tmp_path / "ok.txt"
        f.write_text("hello")
        assert safe_read_file_bytes_nolink(str(f)) == b"hello"

    def test_helper_refuses_a_leaf_swapped_into_a_symlink(self, tmp_path, monkeypatch):
        # Planting a link and reading THROUGH it cannot observe the refusal:
        # validate_file_path realpath-collapses the name before the open, so the
        # no-reparse open lands on the real inode and the helper legitimately
        # returns its bytes (already pinned by
        # test_safe_read_file_bytes_descriptor's benign-leaf-link case). The
        # TOCTOU shape the final-component guard exists for is a link planted AT
        # the canonical name between validation and open, so inject the refusal
        # at that seam instead — patching platform_compat rather than os.open
        # keeps the simulation faithful on Windows, whose arm of that helper
        # reaches CreateFileW with FILE_FLAG_OPEN_REPARSE_POINT and raises the
        # same ELOOP. Without this the OSError would escape as a staging
        # traceback instead of a refusal.
        import errno

        from kiro_crew import platform_compat
        from kiro_crew.hooks import safe_read_file_bytes_nolink
        target = tmp_path / "real.txt"
        target.write_text("x")
        canonical = os.path.realpath(target)
        real_open = platform_compat.open_file_no_reparse

        def eloop(path, *args, **kwargs):
            if os.path.realpath(os.fspath(path)) == canonical:
                raise OSError(errno.ELOOP, "symlink swapped in")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(platform_compat, "open_file_no_reparse", eloop)
        assert safe_read_file_bytes_nolink(str(target)) is None

    def test_staging_uses_nolink_variant(self):
        src = HANDLERS.read_text(encoding="utf-8")
        # the staging tree walk must read via the fd-pinned nolink helper
        # (R33 added within_root= so the call spans lines — match the name +
        # first argument instead of the exact single-line form)
        assert "safe_read_file_bytes_nolink(" in src
        assert "str(src_file)" in src.split("safe_read_file_bytes_nolink(", 1)[1][:120]
        # and must not have regressed to the plain variant in that loop
        walk_block = src.split("os.walk(str(source)", 1)[1][:4000]
        assert "safe_read_file_bytes_nolink" in walk_block
        assert re.search(r"(?<!_nolink)\bsafe_read_file_bytes\(str\(src_file\)\)", walk_block) is None


class TestF2ReaperAccountId:
    def test_reaper_env_supplies_account_id(self):
        import re
        raw = (TEMPLATES / "reaper.yaml").read_text(encoding="utf-8")
        sanitized = re.sub(r"!(Ref|GetAtt|Sub|Not|Equals|If|Select|Join|And|Or|Condition)\b", r"\1", raw)
        doc = yaml.safe_load(sanitized)
        fn = doc["Resources"]["ReaperFn"]["Properties"]
        env = fn["Environment"]["Variables"]
        assert "ACCOUNT_ID" in env, "reaper Lambda must receive ACCOUNT_ID (role cannot call STS)"
        assert "AccountId" in str(env["ACCOUNT_ID"])
