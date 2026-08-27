"""Channel-neutral extraction of local file references from an outbound reply.

Focused on the guarantees a transport depends on:

* a reference inside a code fence is literal text and is never extracted
* only an absolute path to a real, non-sensitive, non-symlinked regular file
  whose LEADING BYTES are a raster can become an upload
* every reference that cannot be sent produces a reason, and keeps its markup so
  the path stays visible in the message
* the rewritten text loses the markup and nothing else
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from kiro_crew import image_artifacts
from kiro_crew.messaging.outbound_files import (
    REASON_MISSING,
    REASON_NOT_ABSOLUTE,
    REASON_NOT_RASTER,
    REASON_OVER_BYTE_BUDGET,
    REASON_OVER_FILE_BYTES,
    REASON_OVER_FILE_CAP,
    REASON_SENSITIVE,
    REASON_SYMLINK,
    REASON_UNREADABLE,
    REMOTE_PREFIXES,
    ExtractLimits,
    OutboundFile,
    Rejection,
    extract_local_refs,
    extract_local_refs_off_loop,
    local_destination,
    md_destination,
    strip_url_syntax,
    unescape_md,
)
from kiro_crew.messaging.split import iter_fence_spans

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
_GIF = b"GIF89a" + b"\x00" * 32
_BMP = b"BM" + b"\x00" * 32
_WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 16


def _png(tmp_path: Path, name: str = "shot.png", body: bytes = _PNG) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return p


class TestExtraction:
    def test_a_lone_image_becomes_a_file_and_leaves_no_markup(self, tmp_path: Path) -> None:
        p = _png(tmp_path)
        result = extract_local_refs(f"Here it is:\n\n![The chart]({p})\n\nDone.")

        assert [f.path for f in result.files] == [str(p)]
        assert result.files[0].alt == "The chart"
        assert result.files[0].mime == "image/png"
        assert result.files[0].data == _PNG
        assert result.files[0].size_bytes == p.stat().st_size
        assert result.rejections == []
        # The image line is gone, and so is the blank line that would otherwise
        # double up where it sat.
        assert result.rewritten_text == "Here it is:\n\nDone."

    def test_every_image_in_the_message_is_extracted(self, tmp_path: Path) -> None:
        first = _png(tmp_path, "a.png")
        second = _png(tmp_path, "b.png", _JPEG)
        result = extract_local_refs(f"one\n\n![a]({first})\n\n![b]({second})\n\ntwo")

        assert [f.path for f in result.files] == [str(first), str(second)]
        assert [f.mime for f in result.files] == ["image/png", "image/jpeg"]
        assert result.rewritten_text == "one\n\ntwo"

    def test_two_images_on_one_line_are_both_seen(self, tmp_path: Path) -> None:
        """A destination pattern that swallows to end-of-line loses the second."""
        first = _png(tmp_path, "a.png")
        second = _png(tmp_path, "b.png")
        result = extract_local_refs(f"![a]({first}) ![b]({second})")

        assert [f.path for f in result.files] == [str(first), str(second)]
        # The line held both images and the space between them, so nothing of it
        # remains to send.
        assert result.rewritten_text == ""

    @pytest.mark.parametrize(
        "body,mime",
        [
            (_PNG, "image/png"),
            (_JPEG, "image/jpeg"),
            (_GIF, "image/gif"),
            (_BMP, "image/bmp"),
            (_WEBP, "image/webp"),
        ],
    )
    def test_every_raster_type_is_accepted(self, tmp_path: Path, body: bytes, mime: str) -> None:
        p = _png(tmp_path, "f.png", body)
        result = extract_local_refs(f"![x]({p})")
        assert [f.mime for f in result.files] == [mime]

    def test_the_type_comes_from_the_bytes_not_the_extension(self, tmp_path: Path) -> None:
        """A PNG saved as .txt is still a PNG, and is sent as one."""
        p = _png(tmp_path, "notes.txt", _PNG)
        result = extract_local_refs(f"![x]({p})")
        assert [f.mime for f in result.files] == ["image/png"]

    def test_alt_text_is_unescaped_and_kept(self, tmp_path: Path) -> None:
        p = _png(tmp_path)
        result = extract_local_refs(rf"![Revenue \[Q1\]]({p})")
        assert result.files[0].alt == "Revenue [Q1]"

    def test_an_empty_alt_is_empty_not_missing(self, tmp_path: Path) -> None:
        p = _png(tmp_path)
        result = extract_local_refs(f"![]({p})")
        assert result.files[0].alt == ""

    def test_no_images_returns_the_text_untouched(self) -> None:
        text = "just prose, and a [link](https://example.com)"
        assert extract_local_refs(text).rewritten_text == text

    def test_empty_text_is_handled(self) -> None:
        result = extract_local_refs("")
        assert result.rewritten_text == ""
        assert result.files == [] and result.rejections == []

    def test_remote_urls_are_left_alone_without_a_rejection(self) -> None:
        text = "![a](https://example.com/x.png) ![b](data:image/png;base64,AAAA) ![c](//cdn/x.png)"
        result = extract_local_refs(text)
        assert result.files == []
        assert result.rejections == []
        assert result.rewritten_text == text

    def test_unclosed_markup_is_left_alone(self, tmp_path: Path) -> None:
        text = f"![a]({tmp_path / 'shot.png'}"
        result = extract_local_refs(text)
        assert result.files == [] and result.rejections == []
        assert result.rewritten_text == text

    @pytest.mark.parametrize("slashes,escaped", [(1, True), (2, False), (3, True), (4, False)])
    def test_backslash_parity_controls_image_markers(
        self, tmp_path: Path, slashes: int, escaped: bool
    ) -> None:
        from kiro_crew.messaging.outbound_files import open_ref_start

        path = _png(tmp_path)
        prefix = "\\" * slashes
        text = prefix + f"![x]({path})"
        result = extract_local_refs(text)
        assert bool(result.files) is not escaped
        assert result.rewritten_text == (text if escaped else prefix)
        assert open_ref_start(prefix + "![x](/tmp/open") == (None if escaped else slashes)


class TestDestinationForms:
    """Destination shapes markdown allows, each reaching the filesystem intact."""

    def test_balanced_parens_in_the_filename(self, tmp_path: Path) -> None:
        p = _png(tmp_path, "screenshot(1).png")
        result = extract_local_refs(f"![x]({p})")
        assert [f.path for f in result.files] == [str(p)]

    def test_angle_wrapped_path_with_spaces(self, tmp_path: Path) -> None:
        p = _png(tmp_path, "generated images/chart.png")
        result = extract_local_refs(f"![x](<{p}>)")
        assert [f.path for f in result.files] == [str(p)]

    def test_title_suffix_is_not_part_of_the_path(self, tmp_path: Path) -> None:
        p = _png(tmp_path)
        result = extract_local_refs(f'![x]({p} "a title")')
        assert [f.path for f in result.files] == [str(p)]
        assert result.rewritten_text == ""

    def test_query_and_fragment_are_stripped(self, tmp_path: Path) -> None:
        p = _png(tmp_path)
        result = extract_local_refs(f"![x]({p}?v=2#top)")
        assert [f.path for f in result.files] == [str(p)]

    def test_file_uri_is_accepted(self, tmp_path: Path) -> None:
        p = _png(tmp_path)
        result = extract_local_refs(f"![x](file://{p})")
        assert [f.path for f in result.files] == [str(p)]

    def test_the_walker_is_the_one_image_artifacts_uses(self) -> None:
        """Both directions share this parser, so it stays exercised from here too."""
        assert md_destination("/tmp/screenshot(1).png)") == "/tmp/screenshot(1).png"
        assert md_destination(r"C:\Users\me\shot.png)") == r"C:\Users\me\shot.png"
        assert md_destination("/tmp/a.png") is None
        for control in "\n\r\0\x1f\x7f":
            assert md_destination(f"/tmp/a{control}b.png)") is None
        assert unescape_md(r"a \[b\]") == "a [b]"

    def test_normalization_is_shared_with_image_artifacts(self) -> None:
        """One normalizer, so the two directions cannot disagree on a path."""
        assert image_artifacts.strip_url_syntax is strip_url_syntax
        assert image_artifacts.local_destination is local_destination
        assert image_artifacts.REMOTE_PREFIXES is REMOTE_PREFIXES
        assert strip_url_syntax("file:///tmp/a.png?v=2#top") == "/tmp/a.png"
        assert local_destination("./rel.png") is None


class TestOutboundSecurity:
    def test_untrusted_windows_unc_is_rejected_before_path_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.messaging import outbound_files as module

        monkeypatch.setattr(module, "os", type("OS", (), {"name": "nt"})(), raising=False)
        monkeypatch.setattr(
            module, "is_unc_shape", lambda raw: raw.startswith(("//", "\\\\")), raising=False
        )
        monkeypatch.setattr(module, "unc_probe_allowed", lambda raw: False, raising=False)
        monkeypatch.setattr(module, "Path", lambda raw: pytest.fail(f"path constructed: {raw}"))

        assert module.local_destination("file:////attacker/share/a.png") is None

    def test_credential_in_raster_metadata_is_rejected(self, tmp_path: Path) -> None:
        p = _png(tmp_path, body=_PNG + b"tEXtComment\0AKIAIOSFODNN7EXAMPLE")
        text = f"![x]({p})"

        result = extract_local_refs(text)

        assert result.files == []
        assert result.rewritten_text == text
        assert result.rejections[0].reason == REASON_SENSITIVE

    @pytest.mark.parametrize("scanner", ["redact_credentials", "redact_exfiltration_urls"])
    def test_a_payload_scanner_failure_rejects_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scanner: str
    ) -> None:
        from kiro_crew.messaging import outbound_files as module

        p = _png(tmp_path)
        text = f"![x]({p})"

        def fail(_text: str) -> tuple[str, list[str]]:
            raise RuntimeError("scanner unavailable")

        monkeypatch.setattr(module, scanner, fail, raising=False)
        result = extract_local_refs(text)

        assert result.files == []
        assert result.rewritten_text == text
        assert result.rejections[0].reason == REASON_SENSITIVE


class TestFences:
    """An image inside a code fence is documentation, not a picture to send."""

    def test_code_images_are_literal(self, tmp_path: Path) -> None:
        m = f"![x]({_png(tmp_path)})"
        cases = (f"    {m}", f"\t{m}", f"~~~\n`\n~~~\n`{m}`\n``example\n{m}\n``")
        for text in cases:
            result = extract_local_refs(text)
            assert result.files == [] and result.rejections == []
            assert result.rewritten_text == text

    def test_tilde_fenced_image_is_literal(self, tmp_path: Path) -> None:
        p = _png(tmp_path)
        text = f"~~~\n![x]({p})\n~~~"
        result = extract_local_refs(text)
        assert result.files == []
        assert result.rewritten_text == text

    def test_a_shorter_run_does_not_close_a_longer_fence(self, tmp_path: Path) -> None:
        """A ``` line inside a ````diff block is content, not a closer."""
        p = _png(tmp_path)
        text = f"````diff\n```\n![x]({p})\n```\n````"
        result = extract_local_refs(text)
        assert result.files == []
        assert result.rewritten_text == text

    def test_a_longer_run_does_close_a_shorter_fence(self, tmp_path: Path) -> None:
        p = _png(tmp_path)
        text = f"```\ncode\n`````\n![x]({p})"
        result = extract_local_refs(text)
        assert [f.path for f in result.files] == [str(p)]

    def test_a_tilde_run_does_not_close_a_backtick_fence(self, tmp_path: Path) -> None:
        p = _png(tmp_path)
        text = f"```\n~~~\n![x]({p})\n```"
        result = extract_local_refs(text)
        assert result.files == []

    def test_an_unclosed_fence_swallows_the_rest_of_the_message(self, tmp_path: Path) -> None:
        p = _png(tmp_path)
        text = f"```python\nprint(1)\n![x]({p})"
        result = extract_local_refs(text)
        assert result.files == []
        assert result.rewritten_text == text

    def test_inline_backticks_in_prose_are_not_a_fence(self, tmp_path: Path) -> None:
        """``` with an info string containing a backtick is prose about fencing."""
        p = _png(tmp_path)
        text = f"``` `x` ```\n![x]({p})"
        result = extract_local_refs(text)
        assert [f.path for f in result.files] == [str(p)]

    def test_four_space_indent_is_not_a_fence_opener(self, tmp_path: Path) -> None:
        p = _png(tmp_path)
        text = f"    ```\n![x]({p})"
        result = extract_local_refs(text)
        assert [f.path for f in result.files] == [str(p)]

    def test_an_image_after_a_closed_fence_is_extracted(self, tmp_path: Path) -> None:
        p = _png(tmp_path)
        result = extract_local_refs(f"```\ncode\n```\n\n![x]({p})")
        assert [f.path for f in result.files] == [str(p)]
        assert result.rewritten_text == "```\ncode\n```"


class TestRejections:
    """Nothing is dropped in silence, and rejected markup stays in the text."""

    def test_relative_paths_are_rejected(self) -> None:
        text = "![x](./shot.png)"
        result = extract_local_refs(text)
        assert result.files == []
        assert result.rejections[0].reason == REASON_NOT_ABSOLUTE
        assert result.rejections[0].dest == "./shot.png"
        assert result.rewritten_text == text  # the path stays visible

    def test_a_missing_file_is_rejected(self, tmp_path: Path) -> None:
        result = extract_local_refs(f"![x]({tmp_path / 'gone.png'})")
        assert result.files == []
        assert result.rejections[0].reason == REASON_MISSING

    def test_a_sensitive_path_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The credential denylist applies to an upload exactly as to a read."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        secret = _png(tmp_path, ".aws/credentials.png")
        result = extract_local_refs(f"![x]({secret})")
        assert result.files == []
        assert result.rejections[0].reason == REASON_SENSITIVE

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need elevation on Windows")
    @pytest.mark.parametrize("exists", [True, False])
    def test_sensitive_parent_alias_is_denied_before_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exists: bool
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        sensitive, root = tmp_path / ".aws", tmp_path / "workspace"
        sensitive.mkdir()
        root.mkdir()
        target = sensitive / "credentials.png"
        if exists:
            target.write_bytes(_PNG)
        (root / "alias").symlink_to(sensitive, target_is_directory=True)
        result = extract_local_refs(f"![x]({root / 'alias' / target.name})", within_root=root)
        assert result.files == []
        assert result.rejections[0].reason == REASON_SENSITIVE

    @pytest.mark.skipif(
        sys.platform == "win32", reason="symlink creation needs elevation on Windows"
    )
    def test_a_symlink_is_rejected_rather_than_resolved(self, tmp_path: Path) -> None:
        """The path handed to a transport must name the file that was validated."""
        real = _png(tmp_path, "real.png")
        link = tmp_path / "link.png"
        link.symlink_to(real)
        result = extract_local_refs(f"![x]({link})")
        assert result.files == []
        assert result.rejections[0].reason == REASON_SYMLINK

    @pytest.mark.skipif(sys.platform == "win32", reason="os.link needs privileges on Windows")
    def test_a_hardlinked_inode_is_rejected(self, tmp_path: Path) -> None:
        """A second name on the inode means the read gate cannot vouch for it."""
        real = _png(tmp_path, "real.png")
        os.link(real, tmp_path / "second.png")
        result = extract_local_refs(f"![x]({real})")
        assert result.files == []
        assert result.rejections[0].reason == REASON_UNREADABLE

    def test_a_directory_is_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / "pics.png"
        target.mkdir()
        result = extract_local_refs(f"![x]({target})")
        assert result.files == []
        assert result.rejections[0].reason == REASON_MISSING

    def test_a_non_raster_behind_an_image_extension_is_rejected(self, tmp_path: Path) -> None:
        """The CWE-434 case: the extension claims PNG, the bytes are a script."""
        p = _png(tmp_path, "payload.png", b"#!/bin/sh\nrm -rf /\n")
        result = extract_local_refs(f"![x]({p})")
        assert result.files == []
        assert result.rejections[0].reason == REASON_NOT_RASTER

    def test_svg_is_rejected(self, tmp_path: Path) -> None:
        """SVG is scriptable markup, not a raster."""
        p = _png(tmp_path, "chart.svg", b'<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        result = extract_local_refs(f"![x]({p})")
        assert result.files == []
        assert result.rejections[0].reason == REASON_NOT_RASTER

    def test_riff_that_is_not_webp_is_rejected(self, tmp_path: Path) -> None:
        p = _png(tmp_path, "a.webp", b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 16)
        result = extract_local_refs(f"![x]({p})")
        assert result.files == []

    def test_a_rejected_file_does_not_block_a_good_one(self, tmp_path: Path) -> None:
        good = _png(tmp_path, "good.png")
        result = extract_local_refs(f"![a](./bad.png)\n\n![b]({good})")
        assert [f.path for f in result.files] == [str(good)]
        assert len(result.rejections) == 1
        assert "./bad.png" in result.rewritten_text


class TestCaps:
    def test_the_file_count_is_capped_with_one_aggregate_reason(self, tmp_path: Path) -> None:
        paths = [_png(tmp_path, f"{i}.png") for i in range(4)]
        text = "\n\n".join(f"![{i}]({p})" for i, p in enumerate(paths))
        result = extract_local_refs(text, limits=ExtractLimits(max_files=2))

        assert [f.path for f in result.files] == [str(paths[0]), str(paths[1])]
        assert [r.reason for r in result.rejections] == [REASON_OVER_FILE_CAP]
        assert str(result.rejections[0]) == (
            "[2 more file reference(s) not sent — limit 2 per message]"
        )
        # Over-cap markup is untouched, so the paths are still readable.
        assert str(paths[2]) in result.rewritten_text
        assert str(paths[3]) in result.rewritten_text

    def test_unreadable_references_consume_the_count_budget(self, tmp_path: Path) -> None:
        """Otherwise a reply full of bad paths does unbounded filesystem work."""
        good = _png(tmp_path, "good.png")
        text = "\n".join(["![a](./one.png)", "![b](./two.png)", f"![c]({good})"])
        result = extract_local_refs(text, limits=ExtractLimits(max_files=2))

        assert result.files == []
        assert len(result.rejections) == 3  # two reasons + the aggregate

    def test_the_byte_budget_stops_at_the_file_that_would_exceed_it(self, tmp_path: Path) -> None:
        first = _png(tmp_path, "a.png")
        second = _png(tmp_path, "b.png")
        budget = first.stat().st_size + second.stat().st_size - 1
        result = extract_local_refs(
            f"![a]({first})\n\n![b]({second})",
            limits=ExtractLimits(max_total_bytes=budget),
        )

        assert [f.path for f in result.files] == [str(first)]
        assert result.rejections[0].reason == REASON_OVER_BYTE_BUDGET
        assert str(second) in result.rewritten_text

    def test_a_later_smaller_file_still_fits(self, tmp_path: Path) -> None:
        big = _png(tmp_path, "big.png", _PNG + b"\x00" * 200)
        small = _png(tmp_path, "small.png")
        result = extract_local_refs(
            f"![a]({big})\n\n![b]({small})",
            limits=ExtractLimits(max_total_bytes=big.stat().st_size - 1),
        )

        assert [f.path for f in result.files] == [str(small)]
        assert len(result.rejections) == 1

    def test_a_per_file_cap_rejects_and_keeps_the_markup(self, tmp_path: Path) -> None:
        """A channel with a low ceiling must not have to drop a stripped file."""
        big = _png(tmp_path, "big.png", _PNG + b"\x00" * 200)
        small = _png(tmp_path, "small.png")
        result = extract_local_refs(
            f"![a]({big})\n\n![b]({small})",
            limits=ExtractLimits(max_file_bytes=small.stat().st_size),
        )

        assert [f.path for f in result.files] == [str(small)]
        assert [r.reason for r in result.rejections] == [REASON_OVER_FILE_BYTES]
        # The reference stays in the text, so the path is still visible.
        assert str(big) in result.rewritten_text
        assert str(small) not in result.rewritten_text

    def test_the_per_file_cap_names_itself_not_the_budget(self, tmp_path: Path) -> None:
        """The tighter bound owns the reason, so a caller knows which to change."""
        p = _png(tmp_path, "big.png", _PNG + b"\x00" * 200)
        result = extract_local_refs(f"![x]({p})", limits=ExtractLimits(max_file_bytes=8))
        assert result.rejections[0].reason == REASON_OVER_FILE_BYTES
        assert "per-file limit" in result.rejections[0].detail

    def test_the_budget_still_owns_its_own_reason(self, tmp_path: Path) -> None:
        """A per-file cap above the remaining budget must not steal the reason."""
        p = _png(tmp_path, "big.png", _PNG + b"\x00" * 200)
        result = extract_local_refs(
            f"![x]({p})",
            limits=ExtractLimits(max_total_bytes=8, max_file_bytes=1024),
        )
        assert result.rejections[0].reason == REASON_OVER_BYTE_BUDGET

    def test_no_per_file_cap_leaves_behaviour_unchanged(self, tmp_path: Path) -> None:
        big = _png(tmp_path, "big.png", _PNG + b"\x00" * 200)
        default = extract_local_refs(f"![x]({big})")
        explicit_none = extract_local_refs(f"![x]({big})", limits=ExtractLimits())
        assert default == explicit_none
        assert [f.path for f in default.files] == [str(big)]
        assert default.rejections == []

    def test_the_defaults_are_the_documented_ones(self) -> None:
        lim = ExtractLimits()
        assert lim.max_files == 12
        assert lim.max_total_bytes == 64 * 1024 * 1024
        assert lim.max_file_bytes is None


class TestRewrite:
    def test_an_inline_remainder_survives(self, tmp_path: Path) -> None:
        p = _png(tmp_path)
        result = extract_local_refs(f"See ![x]({p}) for the numbers.")
        assert result.rewritten_text == "See  for the numbers."

    def test_a_message_that_was_only_an_image_becomes_empty(self, tmp_path: Path) -> None:
        p = _png(tmp_path)
        result = extract_local_refs(f"![x]({p})")
        assert result.rewritten_text == ""
        assert len(result.files) == 1

    def test_a_trailing_image_leaves_no_trailing_blank(self, tmp_path: Path) -> None:
        p = _png(tmp_path)
        result = extract_local_refs(f"Done.\n\n![x]({p})")
        assert result.rewritten_text == "Done."

    def test_a_leading_image_leaves_no_leading_blank(self, tmp_path: Path) -> None:
        p = _png(tmp_path)
        result = extract_local_refs(f"![x]({p})\n\nDone.")
        assert result.rewritten_text == "Done."

    def test_a_wide_authored_gap_keeps_its_width(self, tmp_path: Path) -> None:
        """At most one blank line goes with a dropped line."""
        p = _png(tmp_path)
        result = extract_local_refs(f"a\n\n\n![x]({p})\n\n\nb")
        assert result.rewritten_text == "a\n\n\n\nb"

    def test_surrounding_lines_are_untouched(self, tmp_path: Path) -> None:
        p = _png(tmp_path)
        text = f"# Title\nprose line\n![x]({p})\nmore prose"
        result = extract_local_refs(text)
        assert result.rewritten_text == "# Title\nprose line\nmore prose"

    def test_fenced_content_is_preserved_verbatim(self, tmp_path: Path) -> None:
        p = _png(tmp_path)
        text = f"```\n  indented\n\n  code\n```\n\n![x]({p})"
        result = extract_local_refs(text)
        assert result.rewritten_text == "```\n  indented\n\n  code\n```"
        assert len(result.files) == 1


class TestValidatedBytes:
    """The bytes are the payload, so nothing after extraction can change them."""

    def test_a_post_extraction_swap_cannot_change_what_would_be_uploaded(
        self, tmp_path: Path
    ) -> None:
        """A concurrent writer between extraction and upload must not win.

        A transport handed only a path would re-resolve it and upload whatever is
        there at send time. Carrying the validated bytes closes that window.
        """
        p = _png(tmp_path)
        result = extract_local_refs(f"![x]({p})")
        assert [f.data for f in result.files] == [_PNG]

        # Whatever a writer does afterwards, in place or by replacement.
        p.write_bytes(b"#!/bin/sh\nrm -rf /\n")
        assert result.files[0].data == _PNG

        p.unlink()
        (tmp_path / "shot.png").write_bytes(_JPEG)
        assert result.files[0].data == _PNG
        assert result.files[0].mime == "image/png"

    def test_the_file_may_vanish_entirely(self, tmp_path: Path) -> None:
        """The upload does not depend on the path still existing."""
        p = _png(tmp_path)
        result = extract_local_refs(f"![x]({p})")
        p.unlink()
        assert result.files[0].data == _PNG
        assert result.files[0].path == str(p)  # provenance only

    def test_size_is_derived_from_the_bytes(self, tmp_path: Path) -> None:
        """A separately stored size could disagree with what is sent."""
        body = _PNG + b"\x00" * 111
        p = _png(tmp_path, "big.png", body)
        result = extract_local_refs(f"![x]({p})")
        assert result.files[0].size_bytes == len(body)


class TestRejectionShape:
    def test_str_renders_the_default_prose(self) -> None:
        rejection = Rejection("/tmp/a.png", REASON_MISSING, "no such file")
        assert str(rejection) == "[/tmp/a.png — not sent: no such file]"

    def test_a_message_level_rejection_names_no_destination(self) -> None:
        rejection = Rejection("", REASON_OVER_FILE_CAP, "3 more not sent")
        assert str(rejection) == "[3 more not sent]"

    def test_the_result_carries_a_code_per_refusal_kind(self, tmp_path: Path) -> None:
        """Different refusals are distinguishable without reading the prose."""
        seen = set()
        for text in (
            "![a](./rel.png)",
            f"![b]({tmp_path / 'gone.png'})",
            f"![c]({_png(tmp_path, 'script.png', b'#!/bin/sh')})",
        ):
            seen.update(r.reason for r in extract_local_refs(text).rejections)
        assert seen == {REASON_NOT_ABSOLUTE, REASON_MISSING, REASON_NOT_RASTER}


class TestFenceParity:
    """The extractor's fence decisions ride `split.py`'s machine, not a copy."""

    #: Every fence shape the decoy tests above cover, plus the ones that
    #: distinguish the close rule: run length, fence character, indent.
    _CASES = [
        "```md\n![x](%s)\n```",
        "~~~\n![x](%s)\n~~~",
        "````diff\n```\n![x](%s)\n```\n````",
        "```\ncode\n`````\n![x](%s)",
        "```\n~~~\n![x](%s)\n```",
        "```python\nprint(1)\n![x](%s)",
        "``` `x` ```\n![x](%s)",
        "    ```\n![x](%s)",
        "```\ncode\n```\n\n![x](%s)",
        "![x](%s)\n\n```\ncode\n```",
    ]

    def test_extraction_follows_the_shared_span_walk(self, tmp_path: Path) -> None:
        """Extracted exactly when the shared walk says the markup is not fenced.

        This is the property that makes a second fence implementation
        unnecessary: the extractor asks `split.py` which offsets are code and
        acts on the answer, so there is nothing here that can disagree with the
        splitter about the grammar.
        """
        p = _png(tmp_path)
        for template in self._CASES:
            text = template % p
            offset = text.index("![x](")
            fenced = any(start <= offset < end for start, end in iter_fence_spans(text))
            result = extract_local_refs(text)
            assert bool(result.files) is (not fenced), template
            if fenced:
                # Fenced markup is left byte-for-byte alone.
                assert result.rewritten_text == text, template
                assert result.rejections == [], template

    def test_the_spans_are_ordered_and_disjoint(self, tmp_path: Path) -> None:
        """Overlapping spans would make `_inside` order-dependent."""
        text = "```\na\n```\ntext\n~~~\nb\n~~~\n"
        spans = list(iter_fence_spans(text))
        assert len(spans) == 2
        assert spans[0][1] <= spans[1][0]
        for start, end in spans:
            assert 0 <= start < end <= len(text)


class TestOffLoop:
    def test_a_file_outside_the_approved_root_is_rejected(self, tmp_path: Path) -> None:
        approved = tmp_path / "approved"
        approved.mkdir()
        outside = _png(tmp_path)
        result = extract_local_refs(f"![x]({outside})", within_root=str(approved))
        assert result.files == [] and result.rejections[0].reason == REASON_SENSITIVE

    @pytest.mark.asyncio
    async def test_the_async_form_returns_the_same_result(self, tmp_path: Path) -> None:
        """Async send paths must not stat files on the gateway's event loop."""
        p = _png(tmp_path)
        text = f"Here:\n\n![x]({p})\n\nDone."
        off_loop = await extract_local_refs_off_loop(text, within_root=str(tmp_path))
        assert off_loop == extract_local_refs(text)
        assert [f.path for f in off_loop.files] == [str(p)]

    @pytest.mark.asyncio
    async def test_the_async_form_honours_limits(self, tmp_path: Path) -> None:
        first = _png(tmp_path, "a.png")
        second = _png(tmp_path, "b.png")
        result = await extract_local_refs_off_loop(
            f"![a]({first})\n\n![b]({second})",
            limits=ExtractLimits(max_files=1),
            within_root=str(tmp_path),
        )
        assert [f.path for f in result.files] == [str(first)]
        assert len(result.rejections) == 1


class TestOutboundFile:
    def test_it_is_immutable(self, tmp_path: Path) -> None:
        """A transport must not be able to retarget a validated path."""
        f = OutboundFile(path="/tmp/a.png", data=b"x", alt="a", mime="image/png")
        with pytest.raises(Exception):
            f.path = "/etc/shadow"  # type: ignore[misc]


class TestLinkedAncestorGate:
    """On Windows, a destination beneath a linked ANCESTOR must be refused
    BEFORE ``is_symlink()`` -- that leaf probe is an lstat that resolves every
    ancestor, so the probe itself would traverse the link and open the SMB
    connection the lexical UNC screen exists to prevent (#5962)."""

    def _windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import types

        from kiro_crew.messaging import outbound_files as module

        # Patch ONLY the module's view of os -- patching the global os.name
        # would make pathlib dispatch WindowsPath on a POSIX test host.
        monkeypatch.setattr(module, "os", types.SimpleNamespace(name="nt", path=os.path))

    def test_linked_ancestor_is_refused_before_the_leaf_lstat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ordering IS the property: BOTH downstream probes are wired to
        explode -- is_sensitive_path builds realpath()/resolve() candidate
        forms, so running it first would resolve the chain just like the
        is_symlink lstat would. A regression that probes first fails loudly."""
        from kiro_crew.messaging import outbound_files as module

        p = _png(tmp_path)
        self._windows(monkeypatch)
        monkeypatch.setattr(module, "first_linked_ancestor", lambda _p: str(tmp_path))

        def _boom_lstat(self: Path) -> bool:  # pragma: no cover
            raise AssertionError("is_symlink ran before the ancestor walk")

        def _boom_sensitive(_p: str) -> bool:  # pragma: no cover
            raise AssertionError("is_sensitive_path ran before the ancestor walk")

        monkeypatch.setattr(Path, "is_symlink", _boom_lstat)
        monkeypatch.setattr(module, "is_sensitive_path", _boom_sensitive)
        got = module._inspect(str(p), p, "alt", 10_000, None, None)
        assert isinstance(got, Rejection)
        assert got.reason == REASON_SYMLINK
        # The reply matches the leaf case on purpose: which ancestor is a
        # link is filesystem layout the caller supplied a path to guess at.
        assert got.detail == "symlinks are not uploaded"

    def test_bypassing_the_guard_restores_the_upload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mutation check: with the walk reporting no link, the same file is
        inspected and accepted -- the refusal above is attributable to the
        guard, not to some other screen."""
        from kiro_crew.messaging import outbound_files as module

        p = _png(tmp_path)
        self._windows(monkeypatch)
        monkeypatch.setattr(module, "first_linked_ancestor", lambda _p: None)
        got = module._inspect(str(p), p, "alt", 10_000, None, None)
        assert isinstance(got, OutboundFile)
        assert got.path == str(p)

    def test_the_walk_is_not_consulted_on_posix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On POSIX stat-ing through a symlink is harmless and the real guard
        is the sensitive-path check; an unconditional walk would refuse
        uploads from a symlinked temp dir (macOS /tmp)."""
        if os.name == "nt":
            pytest.skip("gate is active on Windows by design")
        from kiro_crew.messaging import outbound_files as module

        def _boom(_p: object) -> None:  # pragma: no cover
            raise AssertionError("ancestor walk ran on POSIX")

        monkeypatch.setattr(module, "first_linked_ancestor", _boom)
        p = _png(tmp_path)
        got = module._inspect(str(p), p, "alt", 10_000, None, None)
        assert isinstance(got, OutboundFile)

    def test_unc_shaped_expansion_is_refused_lexically(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `~` that expands to a roaming-profile UNC home surfaces a UNC
        shape the raw text did not have; the expanded form must be screened
        (still lexically) before the path reaches any inspecting caller."""
        from kiro_crew.messaging import outbound_files as module

        self._windows(monkeypatch)

        class _ExpandsToUnc:
            def __init__(self, raw: str) -> None:
                self._raw = raw

            def expanduser(self) -> Path:
                return Path("//evil-host/share/x.png")

        monkeypatch.setattr(module, "Path", _ExpandsToUnc)
        assert module.local_destination("~/x.png") is None

    def test_a_leaf_link_is_refused_before_any_resolving_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The walk deliberately excludes the leaf, and the all-platform
        is_symlink() refusal is junction-blind and runs after
        is_sensitive_path (whose candidate forms resolve the leaf) -- so on
        Windows the leaf needs its own junction-aware check first."""
        from kiro_crew.messaging import outbound_files as module

        p = _png(tmp_path)
        self._windows(monkeypatch)
        monkeypatch.setattr(module, "first_linked_ancestor", lambda _p: None)
        monkeypatch.setattr(module, "is_link_or_junction", lambda _p: True)

        def _boom_sensitive(_p: str) -> bool:  # pragma: no cover
            raise AssertionError("is_sensitive_path ran before the leaf link check")

        monkeypatch.setattr(module, "is_sensitive_path", _boom_sensitive)
        got = module._inspect(str(p), p, "alt", 10_000, None, None)
        assert isinstance(got, Rejection)
        assert got.reason == REASON_SYMLINK
        assert got.detail == "symlinks are not uploaded"
