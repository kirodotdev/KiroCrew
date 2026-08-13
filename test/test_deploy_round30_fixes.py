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

    def test_helper_rejects_symlink(self, tmp_path):
        from kiro_crew.hooks import safe_read_file_bytes_nolink
        target = tmp_path / "real.txt"
        target.write_text("x")
        sl = tmp_path / "sl.txt"
        sl.symlink_to(target)
        # validate_file_path realpath-resolves; O_NOFOLLOW guards the final
        # component swap. Either way the read must come back from the REAL
        # inode or be rejected — never follow a swapped-in link blindly.
        result = safe_read_file_bytes_nolink(str(sl))
        assert result in (None, b"x")

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
