"""Source-scan security invariants that span several modules.

Each class reads source or template text and pins a control a security scan
asked for: masked emails in channel inbound logs, no raw exception text in the
Code Review Sage 502 bodies, non-security hashing marked as such, TLS-only S3
bucket policies, and an encrypted reaper alarm topic.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "src" / "kiro_crew" / "deploy" / "skills" / "artifact-deploy" / "templates"


class TestEmailRedactionInLogs:
    """Teams and Webex dispatchers must not log full email addresses."""

    @pytest.mark.parametrize(
        "module_path",
        [
            "src/kiro_crew/teams/transport_dispatch.py",
            "src/kiro_crew/webex/transport_dispatch.py",
        ],
    )
    def test_no_raw_email_in_log_info(self, module_path):
        text = (ROOT / module_path).read_text(encoding="utf-8")
        inbound_calls = [
            node
            for node in ast.walk(ast.parse(text))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "info"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and "inbound from %s" in str(node.args[0].value)
        ]
        # The scan must find the call, or a reworded message makes it vacuous.
        assert inbound_calls, f"no 'inbound from %s' logger.info call in {module_path}"
        for call in inbound_calls:
            who = ast.get_source_segment(text, call.args[1]) or ""
            assert "***" in who, f"Email logged without redaction: {who}"


class TestSageRoutesErrorDisclosure:
    """The 502 catch-all handlers must not return str(e)."""

    def test_no_raw_exception_in_502_response(self):
        src = (
            ROOT / "src" / "kiro_crew" / "apps" / "builtins" / "code_review_sage" / "backend"
        ) / "routes.py"
        text = "\n".join(
            p.read_text(encoding="utf-8")
            for p in [src, *sorted(src.with_name("routes_parts").glob("*.py"))]
        )
        lines_502 = [line for line in text.splitlines() if "status=502" in line]
        for line in lines_502:
            assert "str(e)" not in line, f"str(e) leaked in 502 response: {line.strip()}"


class TestWeakCryptography:
    """MD5 must carry usedforsecurity=False; SHA-1 must be replaced."""

    def test_vector_memory_md5_annotated(self):
        text = (ROOT / "src" / "kiro_crew" / "vector_memory.py").read_text(encoding="utf-8")
        md5_calls = [line for line in text.splitlines() if "hashlib.md5(" in line]
        for line in md5_calls:
            assert (
                "usedforsecurity=False" in line or "usedforsecurity = False" in line
            ), f"MD5 call without usedforsecurity=False: {line.strip()}"

    def test_sage_learning_no_sha1(self):
        src = (
            ROOT / "src" / "kiro_crew" / "apps" / "builtins" / "code_review_sage" / "sage_lib"
        ) / "learning.py"
        text = src.read_text(encoding="utf-8")
        assert "hashlib.sha1(" not in text, "SHA-1 must be replaced with SHA-256"


class TestS3BucketPolicyTLS:
    """Both S3 bucket policies must deny non-HTTPS access."""

    def test_base_stack_has_secure_transport_deny(self):
        text = (TEMPLATES_DIR / "base-stack.yaml").read_text(encoding="utf-8")
        assert "aws:SecureTransport" in text, "Missing aws:SecureTransport condition"
        assert "DenyInsecureTransport" in text, "Missing DenyInsecureTransport statement"
        assert (
            text.count("DenyInsecureTransport") >= 2
        ), "DenyInsecureTransport must appear in both LogBucketPolicy and BucketPolicy"


class TestSNSEncryption:
    """The AlarmTopic must have a KMS key configured for encryption at rest."""

    def test_reaper_alarm_topic_has_kms(self):
        text = (TEMPLATES_DIR / "reaper.yaml").read_text(encoding="utf-8")
        # AWS property name -- wokeignore applied inline in the source
        assert (
            "KmsMasterKeyId" in text  # wokeignore:rule=master
        ), "AlarmTopic missing KMS encryption key"  # wokeignore:rule=master
