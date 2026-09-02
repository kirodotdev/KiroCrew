from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE_REVIEW_WORKFLOW = ROOT / ".github" / "workflows" / "code-review.yml"


def _woke_install_step() -> str:
    workflow = CODE_REVIEW_WORKFLOW.read_text(encoding="utf-8")
    return workflow.split("      - name: Install woke\n", 1)[1].split(
        "      - name: Scan only changed lines\n", 1
    )[0]


def test_woke_install_is_version_pinned_and_checksum_verified():
    install_step = _woke_install_step()

    assert "raw.githubusercontent.com/get-woke/woke/main/install.sh" not in install_step
    assert "| bash" not in install_step
    assert 'WOKE_VERSION: "0.19.0"' in install_step
    assert (
        'WOKE_SHA256: "db5ed0906c81323a8c478cc57e00301dbf184db7a0293d70ba9f4729b6169d8c"'
        in install_step
    )
    assert "releases/download/v${WOKE_VERSION}/${asset}" in install_step
    assert "sha256sum -c -" in install_step
    assert "--strip-components=1 \\" in install_step
    assert '"woke-${WOKE_VERSION}-linux-amd64/woke"' in install_step
    assert 'echo "$bin_dir" >> "$GITHUB_PATH"' in install_step


def test_woke_download_retries_transient_cdn_failures():
    """A single connection reset from the Releases CDN must not fail the gate.

    The Inclusive Language job is a required check that fork contributors
    cannot re-run job-by-job, so an unretried download turns a CDN hiccup into
    a full-pipeline re-roll. ``--retry-all-errors`` is the load-bearing flag:
    plain ``--retry`` does not cover a mid-transfer reset (curl exit 35).
    """
    install_step = _woke_install_step()

    assert "--retry 3" in install_step
    assert "--retry-delay 2" in install_step
    assert "--retry-all-errors" in install_step
    # The checksum gate must survive the retry change, so a partial or
    # substituted download still fails loudly instead of being retried into a
    # bad binary.
    assert "sha256sum -c -" in install_step
