"""CI-only synthetic member recall evidence; never a retrieval score gate."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import sqlite3
import subprocess
import sys
from pathlib import Path


def main() -> None:
    output = Path(os.environ["MEMBER_BENCHMARK_OUTPUT"])
    output.mkdir(parents=True, exist_ok=True)
    provenance_path = output / "provenance.json"
    tracked = (
        subprocess.check_output(
            ["git", "ls-files", "-z", "src/kiro_crew", "scripts/ci-member-memory-benchmark.py"],
            timeout=30,
        )
        .decode("utf-8")
        .split("\0")
    )
    hashes = {}
    native_hashes = {}
    for name in tracked:
        if name and name.endswith(".py"):
            payload = Path(name).read_bytes().replace(b"\r\n", b"\n")
            hashes[name] = hashlib.sha256(payload).hexdigest()
        elif name.startswith("src/kiro_crew/_vendor/llama_cpp_libs/linux_x86_64/"):
            native_hashes[name] = hashlib.sha256(Path(name).read_bytes()).hexdigest()
    versions = {}
    for name in ("numpy", "diskcache", "faiss-cpu"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not installed"
    provenance = {
        "status": "incomplete; no completed measurement is attested",
        "checkout_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], timeout=30)
        .decode("utf-8")
        .strip(),
        "pr_head_commit": os.environ.get("PR_HEAD_SHA", ""),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "runner_image": os.environ.get("ImageVersion", ""),
        "python": sys.version,
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "versions": versions,
        "canonical_source_sha256": hashes,
        "vendored_linux_runtime_sha256": native_hashes,
        "source_canonicalization": "tracked Python files, CRLF replaced with LF, otherwise exact bytes",
        "limitations": "Committed synthetic corpus, not held-out calibration or production answer quality.",
    }

    def save() -> None:
        provenance_path.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")

    save()
    try:
        from kiro_crew import embeddings, vector_memory

        manager = embeddings.ModelDownloadManager()
        provenance["pinned_model_sha256"] = embeddings._GGUF_SHA256
        provenance["accelerators"] = {
            "numpy": vector_memory._HAS_NUMPY,
            "faiss": vector_memory._HAS_FAISS,
        }
        save()
        if not manager.model_ready() and not asyncio.run(manager.ensure_model(attempts=2)):
            raise RuntimeError("Pinned model download did not complete")
        with manager.target.open("rb") as handle:
            actual = hashlib.file_digest(handle, "sha256").hexdigest()
        if actual != embeddings._GGUF_SHA256:
            raise RuntimeError("Resident model does not match the pinned model digest")
        report_path = output / "member-v2-hybrid-qwen3.json"
        # CI supplies the output directory; it and the verified model path are
        # single argv values after fixed flags. The interpreter/module are fixed,
        # and no shell parses these paths (subprocess.run defaults to shell=False).
        subprocess.run(
            [  # noqa: E501  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
                sys.executable,
                "-m",
                "kiro_crew.eval.bench.member_v2",
                "--model-path",
                str(manager.target),
                "--json",
                str(report_path),
            ],
            check=True,
            timeout=540,
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report["model"]["sha256"] != actual:
            raise RuntimeError("Benchmark report used a different model")
        provenance["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
        from kiro_crew.eval.bench.member_v2 import structural_failures

        failures = structural_failures(report)
        provenance["structural_failures"] = failures
        if failures:
            raise RuntimeError("Benchmark structural checks failed: " + "; ".join(failures))
        provenance["status"] = (
            "completed measurement; structural checks passed; scores require review"
        )
    except Exception as exc:
        provenance["status"] = (
            "completed measurement; validation failed"
            if "report_sha256" in provenance
            else "unavailable"
        )
        provenance["error"] = str(exc)
        raise
    finally:
        save()


def validate_completed_structures() -> None:
    """Make measured structural failures authoritative while model availability is optional."""
    from kiro_crew.eval.bench.member_v2 import structural_failures

    output = Path(os.environ["MEMBER_BENCHMARK_OUTPUT"])
    try:
        provenance = json.loads((output / "provenance.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        print("No completed model measurement is available; no structural result is attested.")
        return
    digest = provenance.get("report_sha256")
    if not digest:
        print("Model measurement did not complete; structural evidence is unavailable.")
        return
    report = (output / "member-v2-hybrid-qwen3.json").read_bytes()
    if hashlib.sha256(report).hexdigest() != digest:
        raise RuntimeError("Completed benchmark report does not match its attested digest")
    failures = structural_failures(json.loads(report))
    if failures:
        raise RuntimeError("Measured memory contract failures: " + "; ".join(failures))
    print(
        "Completed measurement passes structural contracts; retrieval scores remain observations."
    )


if __name__ == "__main__":
    if sys.argv[1:] == ["--validate-structures"]:
        validate_completed_structures()
    else:
        main()
