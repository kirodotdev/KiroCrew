"""ACP prompt-block construction and the image capability gate.

Regression coverage for the defect where EVERY channel shipped a filesystem
path as prose: ``AcpSessionHandle.prompt`` hardcoded a single text block, while
the only image encoder lived on ``AcpClient`` -- the path ``AcpProvider.start``
replaces. An image therefore never reached the model as vision input.
"""

from __future__ import annotations

import base64
import io
import json
import os
import random
from pathlib import Path

import pytest

from kiro_crew import hooks
from kiro_crew.acp import prompt_blocks
from kiro_crew.acp.prompt_blocks import (
    _POSIX_PATH_RE,
    IMAGE_MEDIA_TYPES,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_EDGE_PX,
    build_prompt_blocks,
    summarize_prompt_structure,
)

# Smallest valid 1x1 PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _png(tmp_path, name="shot.png"):
    p = tmp_path / name
    p.write_bytes(_PNG)
    return p


class TestBuildPromptBlocks:
    def test_always_returns_at_least_a_text_block(self):
        blocks = build_prompt_blocks("just words")
        assert blocks == [{"type": "text", "text": "just words"}]

    def test_image_path_becomes_an_image_block(self, tmp_path):
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"look at {p} please")

        assert [b["type"] for b in blocks] == ["text", "image"]
        # Text block leads, so the caller can pass this straight to session/prompt.
        assert blocks[0]["text"] == f"look at [image: {p.name}] please"
        assert blocks[1]["mimeType"] == "image/png"
        # The wire carries the BYTES, not the path.
        assert base64.b64decode(blocks[1]["data"]) == _PNG
        assert str(p) not in blocks[1].get("data", "")

    def test_capability_gate_keeps_path_as_text(self, tmp_path):
        """No advertised image capability -> no image block, path left intact.

        Dropping the reference would lose the attachment entirely; leaving the
        path lets a tool-capable agent still open the file.
        """
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"look at {p}", allow_image=False)

        assert [b["type"] for b in blocks] == ["text"]
        assert str(p) in blocks[0]["text"]

    def test_oversized_image_falls_back_to_path(self, tmp_path):
        """Size is checked BEFORE base64: encoding inflates 4/3 and the whole
        request is one newline-delimited JSON frame."""
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"see {p}", max_image_bytes=1)

        assert [b["type"] for b in blocks] == ["text"]
        assert str(p) in blocks[0]["text"]

    def test_missing_and_unreadable_files_are_skipped(self, tmp_path):
        blocks = build_prompt_blocks("/definitely/not/here.png")
        assert [b["type"] for b in blocks] == ["text"]

    def test_directory_with_image_suffix_is_not_read(self, tmp_path):
        d = tmp_path / "weird.png"
        d.mkdir()
        blocks = build_prompt_blocks(f"see {d}")
        assert [b["type"] for b in blocks] == ["text"]

    def test_multiple_images_each_get_a_block(self, tmp_path):
        a = _png(tmp_path, "a.png")
        b = _png(tmp_path, "b.png")
        blocks = build_prompt_blocks(f"{a} and {b}")

        assert [x["type"] for x in blocks] == ["text", "image", "image"]
        assert blocks[0]["text"] == "[image: a.png] and [image: b.png]"

    def test_same_path_twice_is_encoded_once(self, tmp_path):
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"{p} then {p} again")
        # One image block, and both textual occurrences are rewritten.
        assert [x["type"] for x in blocks] == ["text", "image"]
        assert str(p) not in blocks[0]["text"]

    @pytest.mark.parametrize("suffix,mime", sorted(IMAGE_MEDIA_TYPES.items()))
    def test_every_supported_suffix_maps_to_its_mime(self, tmp_path, suffix, mime):
        # Fixtures are format-faithful (real bytes per format, not one PNG
        # renamed): the emitted mimeType tracks the header-DETECTED format,
        # because the backend validates the bytes, not the file extension.
        pil = pytest.importorskip("PIL.Image")
        fmt = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG",
               ".gif": "GIF", ".webp": "WEBP", ".bmp": "BMP"}[suffix]
        p = tmp_path / f"img{suffix}"
        mode = "P" if fmt == "GIF" else "RGB"
        pil.new(mode, (2, 2)).save(p, format=fmt)
        blocks = build_prompt_blocks(f"see {p}")
        assert blocks[1]["mimeType"] == mime

    def test_svg_is_not_inlined(self, tmp_path):
        """SVG is scriptable XML, not a raster image. The direct client listed it
        in its media map while its regex omitted it, so the mapping was already
        unreachable -- keep it excluded deliberately."""
        p = tmp_path / "vector.svg"
        p.write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
        blocks = build_prompt_blocks(f"see {p}")

        assert [b["type"] for b in blocks] == ["text"]
        assert ".svg" not in IMAGE_MEDIA_TYPES

    def test_bare_filename_is_not_treated_as_a_path(self, tmp_path):
        """Only absolute paths are candidates, so prose mentioning a filename
        does not trigger a filesystem probe."""
        blocks = build_prompt_blocks("the file shot.png is attached")
        assert [b["type"] for b in blocks] == ["text"]
        assert blocks[0]["text"] == "the file shot.png is attached"

    def test_default_cap_is_ten_mib(self):
        assert MAX_IMAGE_BYTES == 10 * 1024 * 1024


class TestSensitivePathGate:
    """Image bytes must travel through the centralized sensitive-path gate.

    The gate itself (``hooks.safe_read_file_bytes``: realpath canonicalization,
    ``is_sensitive_path``, ``O_NOFOLLOW``) has its own tests. What matters here
    is that this builder ROUTES through it and honours a refusal -- paths
    reaching it are scraped from message text and so are user-influenced.
    """

    def test_refused_read_is_not_inlined(self, tmp_path, monkeypatch):
        p = _png(tmp_path)
        monkeypatch.setattr(prompt_blocks, "safe_read_file_bytes", lambda raw: None)

        blocks = build_prompt_blocks(f"look at {p}")

        # No image block -- and the path STAYS in the text rather than being
        # silently deleted, so a tool-capable agent can still choose to open it.
        assert [b["type"] for b in blocks] == ["text"]
        assert str(p) in blocks[0]["text"]

    def test_gate_receives_the_path(self, tmp_path, monkeypatch):
        p = _png(tmp_path)
        seen: list[str] = []

        def _spy(raw: str) -> bytes:
            seen.append(raw)
            return _PNG

        monkeypatch.setattr(prompt_blocks, "safe_read_file_bytes", _spy)
        build_prompt_blocks(f"look at {p}")
        assert seen == [str(p)]

    def test_encoded_bytes_come_from_the_gate(self, tmp_path, monkeypatch):
        """The wire payload is the gate's output, not a second unguarded read."""
        pil = pytest.importorskip("PIL.Image")
        p = _png(tmp_path)  # on-disk content is the 1x1 _PNG
        # The gate returns DIFFERENT (valid, within-cap) bytes; prove the wire
        # carries THOSE, not a re-read of the file. A non-image sentinel would be
        # dropped now: undecodable bytes fail closed (see TestImageDownscale).
        buf = io.BytesIO()
        pil.new("RGB", (2, 2), (1, 2, 3)).save(buf, format="PNG")
        gate_bytes = buf.getvalue()
        assert gate_bytes != p.read_bytes()
        monkeypatch.setattr(prompt_blocks, "safe_read_file_bytes", lambda raw: gate_bytes)

        blocks = build_prompt_blocks(f"look at {p}")

        assert base64.b64decode(blocks[1]["data"]) == gate_bytes


class TestPlatformPathGrammar:
    """The path grammar is host-specific on purpose."""

    def test_posix_pattern_matches_posix_paths(self):
        assert prompt_blocks._POSIX_PATH_RE.search("/tmp/a.png") is not None

    def test_posix_pattern_ignores_windows_shapes(self):
        r"""Prose like ``C:\shots\logo.png`` must NOT be a candidate on POSIX.

        Backslash and ``:`` are legal POSIX filename characters, so one merged
        pattern would make a merely-MENTIONED Windows path matchable -- and a
        file with that literal name can exist in the CWD, which would inline
        something the user only talked about.
        """
        assert prompt_blocks._POSIX_PATH_RE.search(r"C:\shots\logo.png") is None
        assert prompt_blocks._POSIX_PATH_RE.search(r"\\host\share\logo.png") is None

    @pytest.mark.parametrize(
        "text",
        [
            r"C:\Users\alice\AppData\Local\Temp\tmpabc.png",
            r"C:/Users/alice/AppData/Local/Temp/tmpabc.png",
            r"\\fileserver\team\diagram.jpg",
            "//fileserver/team/diagram.jpg",
        ],
    )
    def test_windows_pattern_matches_native_absolute_paths(self, text):
        """The shapes the gateway actually produces on Windows.

        The forward-slash UNC form is what the dashboard composer serializes
        into message text (a markdown destination cannot carry raw
        backslashes), and Windows file APIs accept it verbatim.
        """
        assert prompt_blocks._WINDOWS_PATH_RE.search(text) is not None

    def test_windows_pattern_extracts_dashboard_unc_image_markdown(self):
        """A UNC upload serialized by the dashboard must yield the usable path.

        This is the sender-side wire form for a roaming-profile upload: the
        composer emits ``![image](//host/share/...)``. The extracted group must
        be the path itself (openable via ``open()`` on Windows), not a mangled
        span, or the agent silently receives no image block.
        """
        text = "![image](//fileserver/home/me/.kiro/crew/uploads/shot.png)"
        m = prompt_blocks._WINDOWS_PATH_RE.search(text)
        assert m is not None
        assert m.group(1) == "//fileserver/home/me/.kiro/crew/uploads/shot.png"

    def test_windows_pattern_ignores_urls(self):
        """``//`` acceptance must not make ``https://host/x.png`` a candidate."""
        assert prompt_blocks._WINDOWS_PATH_RE.search("see https://example.com/docs/logo.png") is None
        assert prompt_blocks._WINDOWS_PATH_RE.search("see http://host/a.png here") is None

    def test_windows_pattern_requires_an_absolute_path(self):
        assert prompt_blocks._WINDOWS_PATH_RE.search(r"shots\logo.png") is None


class TestUncProbeGate:
    """UNC-shaped candidates must never reach the filesystem un-gated.

    ``Path.is_file()`` on a UNC path makes Windows open an SMB connection to
    the named host, so untrusted message text (``\\\\evil\\share\\x.png`` or
    ``//evil/share/x.png``) would trigger an outbound credential probe. Only
    UNC paths under the gateway's own attachment roots may be probed.
    """

    @pytest.mark.parametrize(
        "raw,want",
        [
            (r"\\host\share\x.png", True),
            ("//host/share/x.png", True),
            (r"C:\Users\me\x.png", False),
            ("C:/Users/me/x.png", False),
            ("/tmp/x.png", False),
        ],
    )
    def test_unc_shape_detection(self, raw, want):
        assert hooks.is_unc_shape(raw) is want

    def test_attacker_host_is_refused(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "kiro_crew.config.paths.data_home", lambda: tmp_path / "home"
        )
        assert hooks.unc_probe_allowed(r"\\evil\share\x.png") is False
        assert hooks.unc_probe_allowed("//evil/share/x.png") is False

    def test_unc_under_a_unc_data_home_is_allowed(self, monkeypatch):
        """Roaming profile: the data home ITSELF is a UNC share."""
        monkeypatch.setattr(
            "kiro_crew.config.paths.data_home",
            lambda: Path(r"\\fileserver\home\me\.kiro\crew"),
        )
        allowed = hooks.unc_probe_allowed(
            r"\\fileserver\home\me\.kiro\crew\uploads\shot.png"
        )
        forward = hooks.unc_probe_allowed(
            "//fileserver/home/me/.kiro/crew/uploads/shot.png"
        )
        # normcase/normpath only fold separators and case on Windows, so the
        # cross-separator equivalence holds there; on POSIX the gate is never
        # consulted (the probe loop is os.name == "nt" scoped).
        if os.name == "nt":
            assert allowed is True
            assert forward is True

    def test_sibling_share_on_same_server_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.config.paths.data_home",
            lambda: Path(r"\\fileserver\home\me\.kiro\crew"),
        )
        if os.name == "nt":
            assert hooks.unc_probe_allowed(r"\\fileserver\other\x.png") is False

    # --- kiro agents dir as a trusted UNC root (#6721) ----------------------
    #
    # Forward-slash UNC spellings are used for every assertion that must hold
    # on the Linux CI box: ``normcase``/``normpath`` leave a ``//host/share``
    # spelling intact on POSIX, so the purely lexical gate behaves exactly as
    # it does on Windows. Backslash spellings only normalize on Windows, so
    # those assertions are additionally guarded like the older tests above.

    _UNC_KIRO_HOME = "//fileserver/profiles/alice/.kiro"

    def _patch_roots(self, monkeypatch, tmp_path, agents_dir):
        """Local data home + the given agents dir, isolating the new root."""
        monkeypatch.setattr("kiro_crew.config.paths.data_home", lambda: tmp_path / "home")
        monkeypatch.setattr("kiro_crew.config.paths.kiro_agents_dir", lambda: agents_dir)

    def test_unc_kiro_agents_dir_is_allowed(self, monkeypatch, tmp_path):
        """Headline #6721: on a roaming-profile (UNC) home, a spec under the
        kiro agents dir passes the gate. The data home is patched LOCAL so the
        admission can only come from the new agents root."""
        self._patch_roots(monkeypatch, tmp_path, Path(self._UNC_KIRO_HOME + "/agents"))
        assert hooks.unc_probe_allowed(self._UNC_KIRO_HOME + "/agents/foo.json") is True
        if os.name == "nt":
            assert (
                hooks.unc_probe_allowed(r"\\fileserver\profiles\alice\.kiro\agents\foo.json")
                is True
            )

    def test_sibling_share_refused_for_agents_root(self, monkeypatch, tmp_path):
        """Same HOST, different share: proves the new root is prefix-anchored,
        not host-anchored."""
        self._patch_roots(monkeypatch, tmp_path, Path(self._UNC_KIRO_HOME + "/agents"))
        assert hooks.unc_probe_allowed("//fileserver/other/agents/foo.json") is False

    def test_neighbour_directory_of_agents_root_refused(self, monkeypatch, tmp_path):
        """``agents-evil`` is not under ``agents``: the comparison must require
        a separator boundary, not a bare ``startswith``."""
        self._patch_roots(monkeypatch, tmp_path, Path(self._UNC_KIRO_HOME + "/agents"))
        assert hooks.unc_probe_allowed(self._UNC_KIRO_HOME + "/agents-evil/foo.json") is False

    def test_local_agents_root_is_not_admitted(self, monkeypatch, tmp_path):
        """On an ordinary local home the added root admits nothing: UNC
        candidates stay refused (the ``is_unc_shape(rootn)`` skip), and the
        root itself is not admitted by exact match either -- the assertion
        that keeps the skip from being dropped for the new root."""
        local_agents = tmp_path / ".kiro" / "agents"
        self._patch_roots(monkeypatch, tmp_path, local_agents)
        assert hooks.unc_probe_allowed("//evil/share/x.png") is False
        assert hooks.unc_probe_allowed(r"\\evil\share\x.png") is False
        assert hooks.unc_probe_allowed(str(local_agents)) is False
        assert hooks.unc_probe_allowed(str(local_agents / "foo.json")) is False

    def test_broken_agents_root_fails_safe(self, monkeypatch):
        """``kiro_agents_dir()`` raising (broken home resolution) must not take
        the gate down: the always-computable roots still apply and the function
        stays total."""

        def boom():
            raise RuntimeError("no usable home")

        monkeypatch.setattr("kiro_crew.config.paths.kiro_agents_dir", boom)
        monkeypatch.setattr(
            "kiro_crew.config.paths.data_home",
            lambda: Path("//fileserver/home/me/.kiro/crew"),
        )
        assert hooks.unc_probe_allowed("//fileserver/home/me/.kiro/crew/uploads/x.png") is True
        assert hooks.unc_probe_allowed("//evil/share/x.png") is False

    def test_agents_root_is_resolved_once_per_configuration(self, monkeypatch, tmp_path):
        """Review finding (#6728 round 2): ``kiro_agents_dir()`` resolves
        ``KIRO_HOME`` (``Path.resolve()`` -- filesystem I/O, SMB on a UNC
        override), so the gate must NOT consult it per check. The root is
        memoized on the raw ``KIRO_HOME`` + accessor identity; repeated gate
        checks under one configuration hit the accessor exactly once."""
        calls: list[int] = []

        def counting_agents_dir():
            calls.append(1)
            return Path(self._UNC_KIRO_HOME + "/agents")

        monkeypatch.setattr("kiro_crew.config.paths.data_home", lambda: tmp_path / "home")
        monkeypatch.setattr("kiro_crew.config.paths.kiro_agents_dir", counting_agents_dir)
        assert hooks.unc_probe_allowed(self._UNC_KIRO_HOME + "/agents/foo.json") is True
        assert hooks.unc_probe_allowed(self._UNC_KIRO_HOME + "/agents/bar.json") is True
        assert hooks.unc_probe_allowed("//evil/share/x.png") is False
        assert len(calls) == 1

    def test_agents_root_failure_is_memoized_not_retried(self, monkeypatch, tmp_path):
        """A failing resolution is memoized as root-absent for the
        configuration: the gate must not re-run a resolve that can block on an
        SMB timeout on every subsequent check."""
        calls: list[int] = []

        def boom():
            calls.append(1)
            raise RuntimeError("no usable home")

        monkeypatch.setattr("kiro_crew.config.paths.data_home", lambda: tmp_path / "home")
        monkeypatch.setattr("kiro_crew.config.paths.kiro_agents_dir", boom)
        assert hooks.unc_probe_allowed("//evil/share/x.png") is False
        assert hooks.unc_probe_allowed("//evil/share/y.png") is False
        assert len(calls) == 1

    class _NtOs:
        """Proxy for the ``os`` module that reports ``name == "nt"``.

        Installed onto ``hooks`` only (``monkeypatch.setattr(hooks, "os", ...)``)
        so ``validate_file_path``'s Windows-only UNC branch runs, while every
        other module keeps the real ``os``: a GLOBAL ``os.name`` patch makes
        ``pathlib.Path.home()`` raise ``RuntimeError`` on this POSIX box, which
        crashes ``security.is_sensitive_path``'s cache-key derivation.

        ``path.realpath`` is additionally stubbed to a LEXICAL no-op (review
        finding): the simulated-Windows tests must never hand the synthetic
        UNC host to a real resolver -- on a Windows host that resolution is
        itself the outbound SMB probe the gate exists to prevent.
        """

        name = "nt"

        class _LexicalPath:
            @staticmethod
            def realpath(p):
                return p

            def __getattr__(self, attr):
                return getattr(os.path, attr)

        path = _LexicalPath()

        def __getattr__(self, attr):
            return getattr(os, attr)

    @pytest.mark.skipif(
        os.name == "nt",
        reason="simulates the Windows resolver on POSIX; on a real Windows host "
        "the downstream resolution of the synthetic UNC host would itself be "
        "the outbound SMB probe the gate exists to prevent",
    )
    def test_validate_file_path_accepts_unc_agents_spec(self, monkeypatch, tmp_path):
        """The surrounding gate: hooks' view of ``os`` is shimmed to report
        ``"nt"`` so ``validate_file_path`` consults the UNC gate on the Linux
        CI box; the gate itself is lexical, and the forward-slash spelling is
        the one ``normpath`` preserves on POSIX."""
        self._patch_roots(monkeypatch, tmp_path, Path(self._UNC_KIRO_HOME + "/agents"))
        monkeypatch.setattr(hooks, "os", self._NtOs())
        assert hooks.validate_file_path(self._UNC_KIRO_HOME + "/agents/foo.json") is not None
        assert hooks.validate_file_path("//evil/share/x.png") is None

    @pytest.mark.skipif(
        os.name == "nt",
        reason="simulates the Windows resolver on POSIX; on a real Windows host "
        "the downstream resolution of the synthetic UNC host would itself be "
        "the outbound SMB probe the gate exists to prevent",
    )
    def test_read_agent_spec_under_unc_agents_dir(self, monkeypatch, tmp_path):
        """End-to-end #6721 symptom: a spec under a UNC-shaped kiro agents dir
        parses instead of silently reading as absent (``None``).

        Windows-resolver simulation, stated per the task spec: hooks' view of
        ``os`` is shimmed to report ``"nt"`` (see ``_NtOs``) so
        ``validate_file_path`` consults the UNC gate, and the spec path reaches
        the reader through a stub whose ``resolve`` keeps the UNC spelling --
        on a real roaming-profile host ``Path.resolve`` keeps a UNC path UNC,
        while on this POSIX box it would collapse the leading ``//`` and dodge
        the gate entirely. The double-slash spelling of a real local file IS
        openable on Linux, so everything downstream of the gate (realpath,
        ``O_NOFOLLOW`` open, JSON parse) runs for real.
        """
        from kiro_crew.agent_discovery import _read_agent_spec

        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "foo.json").write_text('{"name": "foo", "model": "m1"}', encoding="utf-8")
        unc_agents = "/" + str(agents)  # //local/... -- UNC-shaped
        unc_spec = unc_agents + "/foo.json"

        class _WindowsResolvedPath:
            """Duck-typed spec path whose ``resolve`` keeps the UNC spelling."""

            name = "foo.json"

            def resolve(self, strict=False):
                return Path(unc_spec)

        self._patch_roots(monkeypatch, tmp_path, Path(unc_agents))
        monkeypatch.setattr(hooks, "os", self._NtOs())
        assert _read_agent_spec(_WindowsResolvedPath()) == {"name": "foo", "model": "m1"}

    def test_untrusted_unc_text_is_never_stat_probed_on_windows(self, monkeypatch):
        """End-to-end: build_prompt_blocks must not touch the filesystem for a
        refused UNC candidate."""
        if os.name != "nt":
            pytest.skip("probe loop is Windows-scoped")
        probed: list[str] = []
        real_is_file = Path.is_file

        def spy(self):  # type: ignore[no-untyped-def]
            probed.append(str(self))
            return real_is_file(self)

        monkeypatch.setattr(Path, "is_file", spy)
        prompt_blocks.build_prompt_blocks(r"look at \\evil\share\x.png please")
        assert not any("evil" in p for p in probed)

    def test_active_pattern_follows_the_host(self):
        expected = (
            prompt_blocks._WINDOWS_PATH_RE if os.name == "nt" else prompt_blocks._POSIX_PATH_RE
        )
        assert prompt_blocks._PATH_RE is expected

    def test_natively_produced_path_is_inlined_on_this_host(self, tmp_path):
        """End-to-end guard against the gap Windows CI exposed.

        ``tmp_path`` yields backslash paths on Windows, which the POSIX-only
        grammar could not match -- so every image silently stayed prose on a
        supported, CI-tested platform.
        """
        p = _png(tmp_path, "native.png")
        blocks = build_prompt_blocks(f"see {p}")
        assert [b["type"] for b in blocks] == ["text", "image"]


class TestPathsAdjacentToUrls:
    r"""A URL in the message must not swallow the appended image path.

    ``slack/events.py`` emits ``"<user text>\n<image path>"``. With ``\s`` in the
    character class (which matches ``\n``) the leading URL chained across the
    newline into the path, so ``see https://x.com/d\n/tmp/a.png`` matched as the
    single nonexistent path ``//x.com/d\n/tmp/a.png`` -- meaning ANY Slack
    message containing a link silently lost its image, and the temp file was
    then deleted at end of turn.
    """

    def test_url_then_newline_then_path(self, tmp_path):
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"see https://example.com/docs\n{p}")
        assert [b["type"] for b in blocks] == ["text", "image"]

    def test_url_then_space_then_path(self, tmp_path):
        """Same defect on one line -- the newline-only fix does not cover this."""
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"see https://example.com/docs {p}")
        assert [b["type"] for b in blocks] == ["text", "image"]

    def test_url_ending_in_an_image_suffix_is_not_a_path(self):
        """A remote URL is not a local file and must not even be a candidate."""
        assert _POSIX_PATH_RE.search("see https://example.com/logo.png") is None

    def test_multiple_urls_do_not_break_a_trailing_path(self, tmp_path):
        p = _png(tmp_path)
        text = f"a https://x.com/1 b http://y.com/2/z\n{p}"
        blocks = build_prompt_blocks(text)
        assert [b["type"] for b in blocks] == ["text", "image"]

    def test_two_images_after_a_url_both_survive(self, tmp_path):
        a = _png(tmp_path, "a.png")
        b = _png(tmp_path, "b.png")
        blocks = build_prompt_blocks(f"ref https://x.com/d\n{a}\n{b}")
        assert [x["type"] for x in blocks] == ["text", "image", "image"]

    def test_newline_is_not_part_of_a_path(self):
        r"""``\n`` must never be inside a captured path."""
        m = _POSIX_PATH_RE.search("/tmp/one\n/tmp/two.png")
        assert m is not None and "\n" not in m.group(1)

    def test_filename_with_spaces_still_matches(self, tmp_path):
        """Horizontal whitespace stays allowed -- this is why `\\s` was used."""
        p = _png(tmp_path, "my shot.png")
        blocks = build_prompt_blocks(f"look at {p}")
        assert [b["type"] for b in blocks] == ["text", "image"]

    def test_path_inside_markdown_image_syntax(self, tmp_path):
        """The dashboard emits `![image](<path>)`."""
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"![image]({p})")
        assert [b["type"] for b in blocks] == ["text", "image"]


def _sized_image(tmp_path, w, h, fmt="PNG", name=None):
    """Write a solid-colour raster of exactly ``w``x``h`` and return its path.

    Solid colour keeps PNG/JPEG/WEBP encodings tiny so they clear the byte gate
    and the downscale (not the byte cap) is what the test exercises.
    """
    pil = pytest.importorskip("PIL.Image")
    ext = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "BMP": "bmp", "GIF": "gif"}[fmt]
    p = tmp_path / (name or f"big.{ext}")
    pil.new("RGB", (w, h), (123, 200, 60)).save(p, format=fmt)
    return p


def _decoded_size(block):
    pil = pytest.importorskip("PIL.Image")
    with pil.open(io.BytesIO(base64.b64decode(block["data"]))) as im:
        return im.size


class TestImageDownscale:
    """The server-side dimension backstop: no image reaches kiro-cli over the
    Anthropic many-image cap, whatever channel (or skipped client resize) it
    came from."""

    def test_default_cap_is_2000(self):
        assert MAX_IMAGE_EDGE_PX == 2000

    def test_oversized_image_is_downscaled(self, tmp_path):
        p = _sized_image(tmp_path, 4000, 3000)
        blocks = build_prompt_blocks(f"see {p}")
        w, h = _decoded_size(blocks[1])
        # Longest edge capped, aspect preserved (4000x3000 -> 2000x1500).
        assert max(w, h) <= MAX_IMAGE_EDGE_PX
        assert (w, h) == (2000, 1500)

    def test_portrait_image_downscaled_on_its_long_edge(self, tmp_path):
        p = _sized_image(tmp_path, 1000, 4000)
        blocks = build_prompt_blocks(f"see {p}")
        assert _decoded_size(blocks[1]) == (500, 2000)

    def test_image_within_cap_is_byte_identical(self, tmp_path):
        """At/under the cap the original bytes ride through untouched -- no
        needless re-encode (which would recompress and drift quality)."""
        p = _sized_image(tmp_path, 1600, 1200)
        original = p.read_bytes()
        blocks = build_prompt_blocks(f"see {p}")
        assert base64.b64decode(blocks[1]["data"]) == original

    def test_custom_edge_param_is_honoured(self, tmp_path):
        p = _sized_image(tmp_path, 40, 20)
        blocks = build_prompt_blocks(f"see {p}", max_image_edge=10)
        assert _decoded_size(blocks[1]) == (10, 5)

    def test_edge_zero_disables_downscale(self, tmp_path):
        """The escape hatch: a non-positive cap leaves bytes exactly as-is."""
        p = _sized_image(tmp_path, 4000, 10)
        original = p.read_bytes()
        blocks = build_prompt_blocks(f"see {p}", max_image_edge=0)
        assert base64.b64decode(blocks[1]["data"]) == original

    def test_oversized_jpeg_keeps_jpeg_mime(self, tmp_path):
        p = _sized_image(tmp_path, 3000, 1000, fmt="JPEG")
        blocks = build_prompt_blocks(f"see {p}")
        assert blocks[1]["mimeType"] == "image/jpeg"
        assert max(_decoded_size(blocks[1])) <= MAX_IMAGE_EDGE_PX

    def test_oversized_gif_becomes_png_still(self, tmp_path):
        """GIF re-encodes to a PNG first frame: the vision model reads frame 0
        only, and palette rescaling is lossy, so a lossless still is faithful."""
        p = _sized_image(tmp_path, 3000, 100, fmt="GIF")
        blocks = build_prompt_blocks(f"see {p}")
        assert blocks[1]["mimeType"] == "image/png"
        assert max(_decoded_size(blocks[1])) <= MAX_IMAGE_EDGE_PX

    def test_oversized_bmp_is_capped(self, tmp_path):
        # A thin BMP stays under the 10 MB byte gate yet over the edge cap, so
        # the downscale (not the byte gate) is what fires.
        p = _sized_image(tmp_path, 2400, 80, fmt="BMP")
        blocks = build_prompt_blocks(f"see {p}")
        assert max(_decoded_size(blocks[1])) <= MAX_IMAGE_EDGE_PX

    def test_oversized_webp_keeps_webp_mime(self, tmp_path):
        p = _sized_image(tmp_path, 3000, 1000, fmt="WEBP")
        blocks = build_prompt_blocks(f"see {p}")
        assert blocks[1]["mimeType"] == "image/webp"
        assert max(_decoded_size(blocks[1])) <= MAX_IMAGE_EDGE_PX

    def test_undecodable_oversized_image_is_not_inlined(self, tmp_path, monkeypatch):
        """Fail CLOSED: an oversized raster we cannot shrink (here a Pillow
        decompression-bomb rejection) must NOT reach the model as the original
        >2000px payload -- that is exactly what poisons the session. The path is
        left as text instead."""
        pil = pytest.importorskip("PIL.Image")
        p = _sized_image(tmp_path, 2001, 2001)  # over the edge cap
        # Force a decompression-bomb ERROR on decode/resize (pixels > 2 x limit).
        monkeypatch.setattr(pil, "MAX_IMAGE_PIXELS", 1_000_000)
        blocks = build_prompt_blocks(f"see {p}")
        assert [b["type"] for b in blocks] == ["text"]  # no image block
        assert str(p) in blocks[0]["text"]  # path preserved for a tool-capable agent

    def test_exif_orientation_is_baked_on_downscale(self, tmp_path):
        """A re-encode drops the EXIF orientation tag, so orientation must be
        baked into the pixels first or the model sees a rotated photo."""
        pil = pytest.importorskip("PIL.Image")
        p = tmp_path / "rot.jpg"
        img = pil.new("RGB", (3000, 1000), (10, 20, 30))
        exif = img.getexif()
        exif[0x0112] = 6  # Orientation = rotate 90 CW -> displayed as 1000x3000
        img.save(p, format="JPEG", exif=exif)
        blocks = build_prompt_blocks(f"see {p}")
        with pil.open(io.BytesIO(base64.b64decode(blocks[1]["data"]))) as out:
            w, h = out.size
            assert 0x0112 not in out.getexif()  # tag baked away, not carried
            assert h > w  # rotation applied to the pixels -> portrait
            assert max(w, h) <= MAX_IMAGE_EDGE_PX


def _noise_image(tmp_path, w, h, name="noise.png", fmt="PNG"):
    """An image that resists compression, so its encoded size tracks pixel count.

    Built from one bytes buffer rather than a list of per-pixel tuples: at
    2400x2400 that list is 5.76M three-tuples, ~390 MiB of transient peak for a
    17 MB image, and it was the largest single-test excursion in the suite. A
    worker's high-water mark is what the memory budget has to reserve for, so a
    spike nobody sees still costs every other worker headroom.
    """
    pil = pytest.importorskip("PIL.Image")
    rnd = random.Random(1234)
    img = pil.frombytes("RGB", (w, h), rnd.randbytes(w * h * 3))
    p = tmp_path / name
    img.save(p, format=fmt)
    return p


class TestImageEncodedBudget:
    """The per-image ENCODED byte ceiling.

    The dimension cap alone is not enough: Bedrock rejects a single image over
    5 MiB base64, and a raster can sit well inside 2000px while encoding past
    that. A rejected image is replayed from history every later turn, so letting
    one through wedges the whole session.
    """

    def test_default_cap_is_5_mib(self):
        assert prompt_blocks.MAX_IMAGE_B64_BYTES == 5 * 1024 * 1024

    def test_b64_len_matches_real_encoding(self):
        from kiro_crew.imaging import _b64_len

        for n in (0, 1, 2, 3, 4, 100, 1023, 4096):
            assert _b64_len(n) == len(base64.b64encode(b"x" * n))

    def test_image_inside_dimension_cap_but_over_budget_is_shrunk(self, tmp_path):
        """The exact production defect: dimensions are already legal, so the
        dimension pass is a no-op, yet the payload still exceeds the wire limit.
        """
        p = _noise_image(tmp_path, 900, 900)
        budget = len(base64.b64encode(p.read_bytes())) // 3
        blocks = build_prompt_blocks(f"see {p}", max_image_b64_bytes=budget)
        assert [b["type"] for b in blocks] == ["text", "image"]
        assert len(blocks[1]["data"]) <= budget
        # Shrunk, not passed through: the bug was inlining the original here.
        assert base64.b64decode(blocks[1]["data"]) != p.read_bytes()
        assert max(_decoded_size(blocks[1])) < 900

    def test_image_within_budget_is_byte_identical(self, tmp_path):
        p = _sized_image(tmp_path, 100, 80)
        original = p.read_bytes()
        blocks = build_prompt_blocks(f"see {p}")
        assert base64.b64decode(blocks[1]["data"]) == original

    def test_unshrinkable_image_falls_back_to_a_path(self, tmp_path):
        """Fail CLOSED: a budget no rendition can meet must leave the path as
        text rather than inline a payload the backend will reject forever."""
        p = _noise_image(tmp_path, 400, 400)
        blocks = build_prompt_blocks(f"see {p}", max_image_b64_bytes=8)
        assert [b["type"] for b in blocks] == ["text"]
        assert str(p) in blocks[0]["text"]

    def test_zero_budget_disables_the_check(self, tmp_path):
        p = _noise_image(tmp_path, 120, 120)
        original = p.read_bytes()
        blocks = build_prompt_blocks(f"see {p}", max_image_b64_bytes=0)
        assert base64.b64decode(blocks[1]["data"]) == original

    def test_budget_applies_after_the_dimension_cap(self, tmp_path):
        """Both caps hold at once -- shrinking for bytes must not reintroduce an
        over-dimension rendition, and vice versa."""
        p = _noise_image(tmp_path, 2400, 2400, name="big.jpg", fmt="JPEG")
        blocks = build_prompt_blocks(f"see {p}", max_image_b64_bytes=400_000)
        assert max(_decoded_size(blocks[1])) <= MAX_IMAGE_EDGE_PX
        assert len(blocks[1]["data"]) <= 400_000

    def test_shrink_floor_is_respected(self, tmp_path):
        """The loop never grinds an image below the usable-accuracy floor; it
        gives up and hands back a path instead."""
        p = _noise_image(tmp_path, 1000, 1000)
        blocks = build_prompt_blocks(f"see {p}", max_image_b64_bytes=64)
        assert [b["type"] for b in blocks] == ["text"]


class TestSummarizePromptStructure:
    """Content-free outbound-request STRUCTURE diagnostics (issue #6022).

    The summary lets an operator tell a stale/invalid model id apart from a
    structurally malformed payload the next time a turn is rejected as
    "Improperly formed request" -- WITHOUT ever recording message content, so
    it is safe to log even though the kiro-cli data dir holds SSO tokens.
    """

    def test_counts_text_and_image_blocks(self):
        blocks = [
            {"type": "text", "text": "hello"},
            {"type": "image", "data": "x", "mimeType": "image/png"},
            {"type": "image", "data": "y", "mimeType": "image/png"},
        ]
        out = summarize_prompt_structure(blocks)
        assert out["block_count"] == 3
        assert out["type_counts"]["text"] == 1
        assert out["type_counts"]["image"] == 2

    def test_counts_empty_text_blocks(self):
        blocks = [
            {"type": "text", "text": "real content"},
            {"type": "text", "text": "   "},
            {"type": "text", "text": ""},
            {"type": "text", "text": "\n\t"},
        ]
        out = summarize_prompt_structure(blocks)
        assert out["block_count"] == 4
        # Three of the four text blocks are blank/whitespace-only.
        assert out["empty_text_blocks"] == 3

    def test_text_block_missing_text_key_counts_as_empty(self):
        """A ``{"type": "text"}`` with no ``text`` key at all is as
        structurally suspect as one whose ``text`` is a blank string, so it
        folds into the empty count alongside present-but-blank text. This is
        exactly the malformed-payload signal the diagnostic exists to surface.
        """
        blocks = [
            {"type": "text", "text": "real content"},
            {"type": "text"},  # no text key at all
            {"type": "text", "text": None},  # present but not a string
            {"type": "text", "text": "   "},  # present but blank
        ]
        out = summarize_prompt_structure(blocks)
        assert out["block_count"] == 4
        assert out["type_counts"]["text"] == 4
        # The missing key, the non-string, and the blank string all count.
        assert out["empty_text_blocks"] == 3

    def test_reports_tool_use_and_tool_result_imbalance(self):
        """A tool_result with no matching tool_use is the classic malformed
        transcript; the two top-level counts expose the imbalance directly."""
        blocks = [
            {"type": "tool_use", "id": "1"},
            {"type": "tool_use", "id": "2"},
            {"type": "tool_result", "tool_use_id": "1"},
        ]
        out = summarize_prompt_structure(blocks)
        assert out["tool_use"] == 2
        assert out["tool_result"] == 1
        assert out["tool_use"] != out["tool_result"]
        assert out["type_counts"]["tool_use"] == 2
        assert out["type_counts"]["tool_result"] == 1

    def test_reports_total_byte_size(self):
        blocks = [{"type": "text", "text": "hello world"}]
        out = summarize_prompt_structure(blocks)
        assert out["total_bytes"] == len(json.dumps(blocks))
        assert out["total_bytes"] > 0

    def test_summary_contains_no_message_content(self):
        """The #6022 hard requirement: a content sentinel placed in a block's
        text must not appear anywhere in repr() of the summary."""
        sentinel = "SENTINEL_SECRET_TOKEN_ghp_deadbeef"
        blocks = [
            {"type": "text", "text": f"please look at {sentinel} now"},
            {"type": "image", "data": sentinel, "mimeType": "image/png"},
            {"type": "tool_use", "id": sentinel, "input": {"arg": sentinel}},
        ]
        out = summarize_prompt_structure(blocks)
        assert sentinel not in repr(out)
        # And the redaction did not cost the shape: still three blocks.
        assert out["block_count"] == 3

    def test_unknown_block_types_fold_into_other(self):
        blocks = [
            {"type": "text", "text": "x"},
            {"type": "audio", "data": "z"},
            {"type": "resource_link", "uri": "file:///x"},
        ]
        out = summarize_prompt_structure(blocks)
        assert out["type_counts"]["text"] == 1
        assert out["type_counts"]["other"] == 2

    def test_malformed_block_list_does_not_raise(self):
        """A diagnostics helper must never break a live turn: odd inputs yield
        a partial/minimal summary instead of propagating an exception."""
        for bad in (
            [{"nonsense": 1}, None],
            "not a list at all",
            None,
            42,
            [None, None, None],
            [{"type": "text"}],  # text key missing
        ):
            out = summarize_prompt_structure(bad)
            assert isinstance(out, dict)
            assert "block_count" in out
            assert "type_counts" in out
            assert "total_bytes" in out

    def test_unserializable_content_still_yields_counts(self):
        """Content that json.dumps cannot serialize must not sink the summary:
        structural counts survive and total_bytes reports -1 (unknown)."""

        class _Unserializable:
            pass

        blocks = [
            {"type": "text", "text": "ok"},
            {"type": "image", "data": _Unserializable()},
        ]
        out = summarize_prompt_structure(blocks)
        assert out["block_count"] == 2
        assert out["type_counts"]["text"] == 1
        assert out["type_counts"]["image"] == 1
        # default=str is applied, so this actually serializes; but if a type
        # ever defeats even that, total_bytes falls back to -1 rather than
        # raising. Assert the summary is coherent either way.
        assert isinstance(out["total_bytes"], int)

    def test_empty_block_list(self):
        out = summarize_prompt_structure([])
        assert out["block_count"] == 0
        assert out["type_counts"] == {}
        assert out["empty_text_blocks"] == 0
        assert out["tool_use"] == 0
        assert out["tool_result"] == 0
        assert out["total_bytes"] == len(json.dumps([]))

    def test_non_list_argument_reports_coherent_size(self):
        """A non-list/tuple argument normalises to an empty block list, and
        ``total_bytes`` measures THAT normalised list -- not the raw argument.
        So the size stays coherent with the counts (``block_count: 0`` reads as
        an empty ``[]``) instead of "0 blocks, N bytes" describing a payload the
        counts claim is empty. This is exactly the malformed-argument path the
        diagnostic exists to serve."""
        empty_bytes = len(json.dumps([]))
        for bad in ("not a list at all", {"type": "text", "text": "x"}, 42, None):
            out = summarize_prompt_structure(bad)
            assert out["block_count"] == 0
            assert out["total_bytes"] == empty_bytes


class TestLinkedAncestorGate:
    """On Windows, a candidate beneath a linked ANCESTOR must be refused
    BEFORE the first filesystem probe -- ``is_file()`` resolves every
    ancestor, so the probe itself would traverse the link and open the SMB
    connection the lexical UNC screen exists to prevent (#5962).

    NOTE: under the module-local os patch, ``_PATH_RE`` keeps the grammar
    chosen at import time, so candidates here stay host-native; the Windows
    CI shard exercises real backslash shapes via ``tmp_path`` (see
    ``test_natively_produced_path_is_inlined_on_this_host``)."""

    def _windows(self, monkeypatch):
        import types

        # Patch ONLY prompt_blocks' view of os (its sole runtime use is the
        # two gates' os.name checks; _PATH_RE was chosen at import time) --
        # patching the global os.name would make pathlib dispatch WindowsPath
        # everywhere on a POSIX test host.
        monkeypatch.setattr(prompt_blocks, "os", types.SimpleNamespace(name="nt"))

    def test_linked_ancestor_candidate_is_refused_before_any_probe(self, tmp_path, monkeypatch):
        """Ordering IS the property: the leaf probe is wired to explode, so a
        regression that probes first fails loudly instead of silently."""
        p = _png(tmp_path)
        self._windows(monkeypatch)
        monkeypatch.setattr(prompt_blocks, "first_linked_ancestor", lambda _p: str(tmp_path))

        def _boom(self):  # type: ignore[no-untyped-def]  # pragma: no cover
            raise AssertionError("is_file ran before the ancestor walk")

        monkeypatch.setattr(Path, "is_file", _boom)
        blocks = build_prompt_blocks(f"see {p}")
        # The candidate is skipped, not inlined; the text keeps the path so
        # the turn still carries a usable reference.
        assert [b["type"] for b in blocks] == ["text"]
        assert str(p) in blocks[0]["text"]

    def test_bypassing_the_guard_restores_the_probe(self, tmp_path, monkeypatch):
        """Mutation check: with the walk reporting no link, the same candidate
        is probed and inlined again -- so the refusal above is attributable to
        the guard, not to some other screen."""
        p = _png(tmp_path)
        self._windows(monkeypatch)
        monkeypatch.setattr(prompt_blocks, "first_linked_ancestor", lambda _p: None)
        blocks = build_prompt_blocks(f"see {p}")
        assert [b["type"] for b in blocks] == ["text", "image"]

    def test_the_walk_is_not_consulted_on_posix(self, tmp_path, monkeypatch):
        """macOS /tmp and /var are symlinks to /private/*; an unconditional
        walk would refuse every image staged in a temp dir there."""
        if os.name == "nt":
            pytest.skip("gate is active on Windows by design")

        def _boom(_p):  # pragma: no cover
            raise AssertionError("ancestor walk ran on POSIX")

        monkeypatch.setattr(prompt_blocks, "first_linked_ancestor", _boom)
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"see {p}")
        assert [b["type"] for b in blocks] == ["text", "image"]

    def test_a_leaf_link_is_refused_before_the_probe(self, tmp_path, monkeypatch):
        """The walk deliberately excludes the leaf, so the leaf gets its own
        junction-aware check -- is_file() FOLLOWS a final-component link."""
        p = _png(tmp_path)
        self._windows(monkeypatch)
        monkeypatch.setattr(prompt_blocks, "first_linked_ancestor", lambda _p: None)
        monkeypatch.setattr(prompt_blocks, "is_link_or_junction", lambda _p: True)

        def _boom(self):  # type: ignore[no-untyped-def]  # pragma: no cover
            raise AssertionError("is_file ran before the leaf link check")

        monkeypatch.setattr(Path, "is_file", _boom)
        blocks = build_prompt_blocks(f"see {p}")
        assert [b["type"] for b in blocks] == ["text"]
