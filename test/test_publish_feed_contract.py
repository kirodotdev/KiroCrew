"""Contract tests for the desktop update feeds (electron-updater metadata).

The mac and Linux publish lanes write electron-updater channel files
(``feed/<channel>/latest-mac.yml`` / ``latest-linux.yml``) that
``website/electron/auto-update.js`` consumes via the generic provider. Four
properties are load-bearing and easy to break with a plausible-looking edit,
and none of them fails at PR time -- they strand installed clients in the
field days later:

* **sha512 is BASE64, never hex.** electron-updater string-compares the
  feed's sha512 against a base64 digest of the downloaded bytes
  (``DownloadedUpdateHelper.hashFile``). Swapping ``openssl -binary |
  base64`` for ``sha512sum`` (hex) keeps the feed parsing fine while every
  download fails checksum verification.
* **files[].url is ABSOLUTE and points at the byte host.** The yml lives on
  the POINTER host (updates.crew.kiro.dev/feed/<channel>/); the artifact
  bytes live on the BYTE host (download.crew.kiro.dev/desktop/...).
  electron-updater's ``newUrlFromBase`` does ``new URL(fileUrl, base)``,
  which ignores the base for absolute urls -- that behaviour is what makes
  the pointer/bytes host split work. A bare filename would resolve against
  ``feed/<channel>/`` and 404.
* **Go-live order: bytes -> feed -> latest alias.** A feed written before
  its bytes hands clients a 403/404 mid-update; a latest alias written
  before the feed points ahead of the go-live switch. Asserted on PARSED
  step indices (never substring presence) so the assertion cannot go
  vacuous when steps are renamed or reordered.
* **Missing artifacts fail loudly.** A silent skip would leave a green run
  serving a stale feed -- the operator believes the channel moved while
  every client still sees the old version.

Cache discipline rides along: immutable versioned keys (max-age=31536000 +
conditional write), short-TTL mutable aliases (max-age=300), no-cache feeds.
And every publishing job declares ``environment: prod`` because the publish
role's OIDC trust accepts exactly ref:refs/heads/main and environment:prod.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
MAC_WORKFLOW = WORKFLOWS / "sign-and-notarize.yml"
LINUX_WORKFLOW = WORKFLOWS / "publish-linux.yml"

# Dummy values used to render the feed heredocs into parseable YAML. The
# sha512 stand-ins are the names of the shell variables the step MUST
# populate from `openssl dgst -sha512 -binary | base64`; if the heredoc
# references anything else the substitution misses and the residual `$`
# fails the render (same discipline as test_nightly_version_contract.py:
# extract the literal format from the workflow instead of duplicating it).
_SUBS = {
    "VERSION": "1.2.3",
    "CHANNEL": "nightly",
    "DESKTOP_KEY": "desktop/nightly/1.2.3/KiroCrew.zip",
    "DMG_KEY": "desktop/nightly/1.2.3/KiroCrew.dmg",
    "APPIMAGE_KEY": "desktop/nightly/1.2.3/KiroCrew-x86_64.AppImage",
    "ZIP_SHA512": "ZIPSHA512BASE64==",
    "DMG_SHA512": "DMGSHA512BASE64==",
    "APPIMAGE_SHA512": "APPIMAGESHA512BASE64==",
    "ZIP_SIZE": "111",
    "DMG_SIZE": "222",
    "APPIMAGE_SIZE": "333",
}
_CDN_BASE = "https://download.crew.kiro.dev"


def _jobs(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]


def _steps(path: Path, job: str) -> list[dict]:
    return _jobs(path)[job]["steps"]


def _step_index(steps: list[dict], name: str) -> int:
    names = [s.get("name", "") for s in steps]
    assert name in names, f"step {name!r} not found; steps are {names}"
    return names.index(name)


def _step(steps: list[dict], name: str) -> dict:
    return steps[_step_index(steps, name)]


def _feed_step(path: Path, job: str) -> dict:
    return _step(_steps(path, job), "Write update feed")


def _heredoc(run_text: str) -> str:
    """The feed document between ``<<EOF`` and its terminator."""
    lines = run_text.splitlines()
    start = next(i for i, line in enumerate(lines) if "<<EOF" in line)
    end = next(i for i, line in enumerate(lines[start + 1 :], start + 1) if line.strip() == "EOF")
    return "\n".join(lines[start + 1 : end])


def _rendered_feed(run_text: str) -> dict:
    """Render the heredoc with dummy values and parse it as YAML."""
    doc = _heredoc(run_text)
    doc = re.sub(r"\$\{\{\s*vars\.CLI_CDN_BASE\s*\}\}", _CDN_BASE, doc)
    doc = re.sub(r"\$\(date[^)]*\)", "2026-07-28T18:00:00Z", doc)
    doc = re.sub(r"\$\{(\w+)\}", lambda m: _SUBS.get(m.group(1), m.group(0)), doc)
    assert "$" not in doc, f"unresolved shell reference in rendered feed:\n{doc}"
    return yaml.safe_load(doc)


# ---------------------------------------------------------------------------
# Key layout: exactly what electron-updater's generic provider parses.
# ---------------------------------------------------------------------------


def _assert_updater_layout(feed: dict) -> None:
    assert set(feed.keys()) == {"version", "files", "path", "sha512", "releaseDate"}, (
        f"feed keys {sorted(feed)} diverge from the electron-updater contract "
        "(version, files, path, sha512, releaseDate)"
    )
    assert feed["version"] == _SUBS["VERSION"], "version must be the raw ${VERSION}, no v-prefix"
    assert isinstance(feed["files"], list) and feed["files"], "files must be a non-empty list"
    for entry in feed["files"]:
        assert set(entry.keys()) == {"url", "sha512", "size"}, (
            f"files[] entry keys {sorted(entry)} diverge from url+sha512+size"
        )
        assert isinstance(entry["size"], int), "size must be an unquoted integer"
    # releaseDate must be QUOTED in the heredoc: unquoted ISO timestamps are
    # YAML-typed as datetime by some parsers, and electron-updater expects a
    # string it can hand to `new Date(...)`.
    assert isinstance(feed["releaseDate"], str)


def test_mac_feed_layout_is_electron_updater_metadata() -> None:
    feed = _rendered_feed(_feed_step(MAC_WORKFLOW, "publish")["run"])
    _assert_updater_layout(feed)
    urls = [f["url"] for f in feed["files"]]
    # MacUpdater requires a zip entry (findFile(files, "zip", ["pkg","dmg"]))
    # -- the ZIP is what Squirrel.Mac consumes; the DMG stays the human
    # first-install download.
    assert any(u.endswith(".zip") for u in urls), "latest-mac.yml must list the update ZIP"
    assert feed["path"].endswith(".zip"), "legacy top-level path must be the ZIP, not the DMG"
    assert feed["sha512"] == _SUBS["ZIP_SHA512"], "top-level sha512 must be the ZIP digest"


def test_linux_feed_layout_is_electron_updater_metadata() -> None:
    feed = _rendered_feed(_feed_step(LINUX_WORKFLOW, "publish-linux")["run"])
    _assert_updater_layout(feed)
    urls = [f["url"] for f in feed["files"]]
    # AppImageUpdater requires an AppImage entry
    # (findFile(files, "AppImage", ["rpm","deb","pacman"])).
    assert any(u.endswith(".AppImage") for u in urls), "latest-linux.yml must list the AppImage"
    assert feed["path"].endswith(".AppImage")
    assert feed["sha512"] == _SUBS["APPIMAGE_SHA512"]


# ---------------------------------------------------------------------------
# sha512 encoding: base64 of the raw digest, never hex.
# ---------------------------------------------------------------------------


def test_feed_sha512_is_base64_not_hex() -> None:
    for path, job in ((MAC_WORKFLOW, "publish"), (LINUX_WORKFLOW, "publish-linux")):
        run = _feed_step(path, job)["run"]
        assert re.search(r"openssl dgst -sha512 -binary[^\n|]*\|\s*base64", run), (
            f"{path.name}: feed sha512 must be computed as "
            "`openssl dgst -sha512 -binary | base64` (raw digest, base64-encoded)"
        )
        assert "sha512sum" not in run, (
            f"{path.name}: sha512sum emits HEX; electron-updater compares "
            "BASE64 and every download would fail checksum verification"
        )


# ---------------------------------------------------------------------------
# Absolute byte-host urls inside a pointer-host feed.
# ---------------------------------------------------------------------------


def test_feed_urls_are_absolute_byte_host_urls() -> None:
    for path, job in ((MAC_WORKFLOW, "publish"), (LINUX_WORKFLOW, "publish-linux")):
        doc = _heredoc(_feed_step(path, job)["run"])
        url_lines = [line.strip() for line in doc.splitlines() if re.match(r"\s*(-\s+)?url:", line)]
        assert url_lines, f"{path.name}: feed heredoc has no files[].url lines"
        for line in url_lines:
            assert "${{ vars.CLI_CDN_BASE }}/" in line, (
                f"{path.name}: {line!r} must be an ABSOLUTE ${{{{ vars.CLI_CDN_BASE }}}} url. "
                "The yml lives on the pointer host (updates.crew.kiro.dev/feed/...); a "
                "relative filename would resolve against feed/<channel>/ and 404. "
                "electron-updater ignores the feed base only for absolute urls."
            )
        # And the rendered urls point at the byte prefix, not the feed prefix.
        feed = _rendered_feed(_feed_step(path, job)["run"])
        for entry in feed["files"]:
            assert entry["url"].startswith(f"{_CDN_BASE}/desktop/"), (
                f"{path.name}: {entry['url']!r} must reference the desktop/ byte prefix"
            )


def test_feed_destination_is_pointer_prefix_yaml() -> None:
    """The yml is uploaded under feed/ (pointer host behavior), not desktop/."""
    for path, job, channel_file in (
        (MAC_WORKFLOW, "publish", "latest-mac.yml"),
        (LINUX_WORKFLOW, "publish-linux", "latest-linux.yml"),
    ):
        run = _feed_step(path, job)["run"]
        assert f"feed/${{CHANNEL}}/{channel_file}" in run, (
            f"{path.name}: feed must upload to feed/<channel>/{channel_file} -- the exact "
            "path website/electron/auto-update.js's provider resolves from the feed base"
        )
        assert "--content-type text/yaml" in run


def test_mac_lane_keeps_the_legacy_json_feed_as_a_transition_bridge() -> None:
    """The retired hand-rolled JSON feed MUST keep being written for now.

    Builds fielded before the electron-updater migration poll
    feed/<channel>/latest-mac.json and know nothing about latest-mac.yml.
    Dropping the JSON write would leave those installs tracking a file that
    never updates again -- permanently unable to discover ANY future version,
    with a manual DMG re-download as the only escape. Shipped clients cannot
    be recalled, so the server keeps speaking the old dialect until the old
    clients are gone.

    This test exists so the bridge cannot be deleted as apparent dead code.
    Removal condition is documented on the workflow step itself: no installs
    older than the first electron-updater release remain (or a deliberate
    decision to abandon stragglers).
    """
    text = MAC_WORKFLOW.read_text(encoding="utf-8")
    assert "latest-mac.json" in text, (
        "the legacy JSON feed write was removed -- this strands every install "
        "fielded before the electron-updater migration"
    )
    steps = _steps(MAC_WORKFLOW, "publish")
    legacy = _step_index(steps, "Write legacy update feed (pre-electron-updater clients)")
    modern = _step_index(steps, "Write update feed")
    print(f"sign-and-notarize.yml feed step indices: yml={modern} legacy-json={legacy}")
    assert modern < legacy, (
        "the canonical yml feed must be written before the legacy bridge so a "
        "partial failure can never leave the legacy feed ahead of the modern one"
    )
    # Linux had no updater before this migration, so it has no old clients and
    # must NOT grow a legacy feed.
    assert "latest-linux.json" not in LINUX_WORKFLOW.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Go-live ordering: bytes -> feed -> latest alias, on parsed step indices.
# ---------------------------------------------------------------------------


def test_mac_publish_order_bytes_then_feed_then_alias() -> None:
    steps = _steps(MAC_WORKFLOW, "publish")
    verify = _step_index(steps, "Verify gated artifact contents")
    zip_pub = _step_index(steps, "Publish notarized artifact to distribution bucket")
    dmg_pub = _step_index(steps, "Publish DMG to distribution bucket")
    feed = _step_index(steps, "Write update feed")
    alias = _step_index(steps, "Update latest DMG alias")
    print(
        f"sign-and-notarize.yml publish step indices: verify={verify} "
        f"zip={zip_pub} dmg={dmg_pub} feed={feed} alias={alias}"
    )
    assert verify < zip_pub < dmg_pub < feed < alias, (
        f"go-live order violated (verify={verify}, zip={zip_pub}, dmg={dmg_pub}, "
        f"feed={feed}, alias={alias}): the feed references BOTH versioned keys so it "
        "must trail them, and the latest alias must never point ahead of the go-live switch"
    )
    # The feed must reference the keys the byte steps exported -- pinning that
    # the ordering above is a data dependency, not a coincidence.
    run = steps[feed]["run"]
    assert "${DESKTOP_KEY}" in run and "${DMG_KEY}" in run


def test_linux_publish_order_bytes_then_feed_then_alias() -> None:
    steps = _steps(LINUX_WORKFLOW, "publish-linux")
    locate = _step_index(steps, "Locate AppImage")
    attest = _step_index(steps, "Attest AppImage provenance")
    bytes_pub = _step_index(steps, "Publish AppImage to distribution bucket")
    feed = _step_index(steps, "Write update feed")
    alias = _step_index(steps, "Update latest AppImage alias")
    print(
        f"publish-linux.yml step indices: locate={locate} attest={attest} "
        f"bytes={bytes_pub} feed={feed} alias={alias}"
    )
    assert locate < attest < bytes_pub < feed < alias, (
        f"go-live order violated (locate={locate}, attest={attest}, bytes={bytes_pub}, "
        f"feed={feed}, alias={alias}): attestation precedes publish so un-attested bytes "
        "never go live; the feed trails the versioned key it references; the alias trails "
        "the go-live switch"
    )
    assert "${APPIMAGE_KEY}" in steps[feed]["run"]


def test_feed_chain_steps_share_one_skip_gate() -> None:
    """Every step in the go-live chain must carry the SAME condition. A feed
    step gated differently from its byte steps could run while the bytes were
    skipped -- advertising artifacts that were never uploaded."""
    for path, job, names in (
        (
            MAC_WORKFLOW,
            "publish",
            (
                "Publish notarized artifact to distribution bucket",
                "Publish DMG to distribution bucket",
                "Write update feed",
                "Update latest DMG alias",
            ),
        ),
        (
            LINUX_WORKFLOW,
            "publish-linux",
            (
                "Publish AppImage to distribution bucket",
                "Write update feed",
                "Update latest AppImage alias",
            ),
        ),
    ):
        steps = _steps(path, job)
        gates = {name: _step(steps, name).get("if") for name in names}
        assert all(g == "env.HAS_SIGNING_SECRETS" for g in gates.values()), (
            f"{path.name}: go-live chain must be all-or-nothing on the same gate, got {gates}"
        )
        for name in names:
            assert "continue-on-error" not in _step(steps, name), (
                f"{path.name}: {name!r} must fail the job, never continue past a failure"
            )


# ---------------------------------------------------------------------------
# Cache TTL discipline per key class.
# ---------------------------------------------------------------------------


def test_versioned_keys_are_immutable_and_conditionally_written() -> None:
    for path, job, name in (
        (MAC_WORKFLOW, "publish", "Publish notarized artifact to distribution bucket"),
        (MAC_WORKFLOW, "publish", "Publish DMG to distribution bucket"),
        (LINUX_WORKFLOW, "publish-linux", "Publish AppImage to distribution bucket"),
    ):
        run = _step(_steps(path, job), name)["run"]
        assert "public, max-age=31536000, immutable" in run, (
            f"{path.name}/{name}: versioned keys are immutable-cached for a year"
        )
        assert "--if-none-match" in run, (
            f"{path.name}/{name}: the conditional write is the never-republish "
            "guarantee -- a republished immutable key diverges across CloudFront edges"
        )


def test_latest_aliases_use_short_ttl_and_plain_overwrite() -> None:
    for path, job, name in (
        (MAC_WORKFLOW, "publish", "Update latest DMG alias"),
        (LINUX_WORKFLOW, "publish-linux", "Update latest AppImage alias"),
    ):
        run = _step(_steps(path, job), name)["run"]
        assert "public, max-age=300" in run, (
            f"{path.name}/{name}: mutable latest aliases roll over within minutes"
        )
        assert "--if-none-match" not in run, (
            f"{path.name}/{name}: aliases are mutable by design; a conditional write "
            "would freeze them at the first publish"
        )


def test_feeds_carry_an_explicit_cache_control() -> None:
    """Every feed write must set an EXPLICIT Cache-Control with a short TTL.

    This encodes the #709 incident rather than a preference. A feed served with
    NO Cache-Control is subject to heuristic freshness (RFC 9111: caches may
    guess a lifetime from Last-Modified age), and macOS clients read it through
    NSURLCache -- which resolved a 22h-stale body and offered the version the
    user was already running, in a loop. An explicit short TTL removes the
    guess. `no-cache` would also work, but max-age=300 is what shipped and what
    fielded clients now receive; keeping the two identical means the legacy
    bridge behaves exactly as it does today.

    The client-side belt (electron-updater's own noCache query param) is NOT a
    substitute: a build already in the field cannot be given a header fix
    retroactively, so the origin header is what lets a poisoned client recover.
    """
    for path, job in ((MAC_WORKFLOW, "publish"), (LINUX_WORKFLOW, "publish-linux")):
        run = _feed_step(path, job)["run"]
        assert "--cache-control" in run, (
            f"{path.name}: feed written without an explicit Cache-Control -- "
            "heuristic freshness caused the #709 stale-feed incident"
        )
        assert re.search(r"max-age=(\d+)", run), (
            f"{path.name}: feed Cache-Control must pin an explicit max-age"
        )
        ttl = int(re.search(r"max-age=(\d+)", run).group(1))
        assert 0 < ttl <= 600, (
            f"{path.name}: feed TTL is {ttl}s -- the feed is the go-live switch, so a "
            "long TTL delays every client's discovery of a release"
        )


def test_mac_legacy_bridge_matches_the_modern_feed_cache_control() -> None:
    """The bridge must not be cached differently from the feed it mirrors.

    Both advertise the same version and the same bytes. Divergent TTLs would
    let an old client and a new client disagree about what the latest version
    is for up to the difference between them.
    """
    steps = _steps(MAC_WORKFLOW, "publish")
    modern = _step_index(steps, "Write update feed")
    legacy = _step_index(steps, "Write legacy update feed (pre-electron-updater clients)")
    modern_cc = re.search(r'--cache-control "([^"]+)"', steps[modern]["run"]).group(1)
    legacy_cc = re.search(r'--cache-control "([^"]+)"', steps[legacy]["run"]).group(1)
    print(f"feed Cache-Control: modern={modern_cc!r} legacy={legacy_cc!r}")
    assert modern_cc == legacy_cc, (
        f"feed and legacy bridge disagree on Cache-Control ({modern_cc!r} vs {legacy_cc!r})"
    )


def test_mac_feed_verifies_the_header_clients_receive() -> None:
    """The publish step must read the header back through the public CDN.

    From #709: `s3api head-object` cannot be used here because the publish role
    is Put-only on feed/* (GetObject is granted on cli/* alone), so a read-back
    would AccessDenied and abort the step AFTER the feed was already published
    -- a guard that fails on permissions instead of on the condition it guards.
    """
    run = _feed_step(MAC_WORKFLOW, "publish")["run"]
    assert "curl" in run and "-I" in run, (
        "feed step must verify the served Cache-Control through the CDN"
    )
    # Strip comment lines before checking for the forbidden call: the step
    # DOCUMENTS why head-object is wrong, so a naive substring match would
    # trip on its own rationale.
    code = "\n".join(
        line for line in run.splitlines() if not line.strip().startswith("#")
    )
    assert "head-object" not in code, (
        "must not verify via s3api head-object -- the publish role lacks GetObject "
        "on feed/*, so the guard would fail on permissions after publishing"
    )


# ---------------------------------------------------------------------------
# environment: prod on every publishing job (OIDC trust subject).
# ---------------------------------------------------------------------------


def test_publishing_jobs_declare_prod_environment() -> None:
    assert _jobs(MAC_WORKFLOW)["publish"].get("environment") == "prod", (
        "sign-and-notarize publish job must declare environment: prod -- the publish "
        "role's OIDC trust accepts exactly ref:refs/heads/main and environment:prod"
    )
    assert _jobs(LINUX_WORKFLOW)["publish-linux"].get("environment") == "prod"


# ---------------------------------------------------------------------------
# Missing artifacts fail loudly (never a silent skip serving a stale feed).
# ---------------------------------------------------------------------------


def test_mac_gated_artifact_contents_fail_loudly_when_missing() -> None:
    steps = _steps(MAC_WORKFLOW, "publish")
    run = _step(steps, "Verify gated artifact contents")["run"]
    for probe in ('[ -f "work/notarized.zip" ]', '[ -f "work/${ARTIFACT_BASENAME}.dmg" ]'):
        assert probe in run, f"gated-artifact verify lost its {probe} check"
    assert "exit 1" in run, "a missing gated artifact must fail the job before any publish"


def test_linux_missing_appimage_fails_loudly() -> None:
    run = _step(_steps(LINUX_WORKFLOW, "publish-linux"), "Locate AppImage")["run"]
    assert "No AppImage found" in run and "exit 1" in run, (
        "a missing AppImage must fail the job -- a silent skip would leave a green "
        "run serving a stale feed"
    )
    assert "Expected exactly one AppImage" in run, (
        "ambiguous artifacts must also fail loudly rather than feeding an arbitrary file"
    )


def test_mac_notarize_attaches_gated_artifact_fail_closed() -> None:
    step = _step(_steps(MAC_WORKFLOW, "notarize"), "Attach notarized artifact to workflow run")
    assert step["with"]["if-no-files-found"] == "error", (
        "the gated artifact upload must error when empty -- it is the publish job's sole input"
    )


# ---------------------------------------------------------------------------
# Installer <-> publisher channel-name agreement
# ---------------------------------------------------------------------------

CLI_INSTALLER = ROOT / "cli.sh"
CLI_WORKFLOW = WORKFLOWS / "publish-cli.yml"

# The channels the publisher accepts, as documented on its workflow_call input.
# Kept as the single source both assertions read, so a channel added to the
# pipeline without teaching the installer about it fails here.
_PUBLISHED_CHANNELS = ("nightly", "insider", "stable")


def _installer_source() -> str:
    return CLI_INSTALLER.read_text(encoding="utf-8")


def _installer_code() -> str:
    """Installer source with comment lines stripped, for code-only assertions."""
    lines = _installer_source().splitlines()
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))


def test_publisher_documents_the_expected_channel_set() -> None:
    """Anchor _PUBLISHED_CHANNELS to the workflow instead of duplicating it."""
    doc = yaml.safe_load(CLI_WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML resolves the bare `on:` key to the boolean True (YAML 1.1).
    described = doc[True]["workflow_call"]["inputs"]["channel"]["description"]
    declared = tuple(part.strip() for part in described.split(":", 1)[1].split("|"))
    assert declared == _PUBLISHED_CHANNELS, (
        "publish-cli.yml's channel set changed; cli.sh's accepted channels and "
        f"_PUBLISHED_CHANNELS must move with it (workflow says {declared})"
    )


def test_installer_channel_name_is_the_literal_path_segment() -> None:
    """The installer must not remap a channel name to a different prefix.

    ``publish-cli.yml`` writes ``feed/${CHANNEL}/latest-cli.json`` and
    ``cli/${CHANNEL}/${VERSION}/`` using the literal channel, and "beta" was
    renamed to "insider" everywhere *including* the path segment. A remap here
    (``insider`` -> ``beta``) makes the installer request a prefix that was
    never published: the CDN answers 403 and the user sees "channel has no
    feed", with no hint that the channel itself is fine.
    """
    code = _installer_code()
    assert re.search(r'^CHANNEL_PATH="\$CHANNEL"$', code, re.M), (
        "cli.sh must use the channel verbatim as the storage prefix"
    )
    assert "beta" not in code, (
        "cli.sh has executable code referencing a `beta` prefix; the published "
        "path segment is `insider` (docs/release-automation.md)"
    )


def test_installer_rejects_an_unknown_channel_before_hitting_the_cdn() -> None:
    """A typo'd channel must fail with the valid set, not an opaque CDN 403."""
    src = _installer_source()
    guard = re.search(r"^case \"\$CHANNEL\" in\n\s*([a-z|]+)\) ;;", src, re.M)
    assert guard, "cli.sh must validate --channel against a known set"
    assert tuple(guard.group(1).split("|")) == _PUBLISHED_CHANNELS, (
        "cli.sh's accepted channels must match what publish-cli.yml publishes"
    )
    # The rejection has to name the alternatives; that message is the whole
    # point of validating locally instead of letting the fetch 403.
    for channel in _PUBLISHED_CHANNELS:
        assert channel in src.split("unknown channel", 1)[1][:200], (
            f"the unknown-channel error must list '{channel}'"
        )
