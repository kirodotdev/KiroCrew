"""Build gate + behaviour tests for the single Slack render pipeline.

``to_slack_mrkdwn`` is not a safe thing to call directly. It strips ANSI escapes
and self-truncates at ``SLACK_MAX_TEXT`` before converting, and each of those
defeats credential redaction from a different side:

- **redact then convert** -- the ANSI strip *reassembles* a credential the
  escapes had broken up, so ``AKIA<esc>IOSF...`` sails past the regex and lands
  on Slack whole.
- **convert then redact** -- the 39,000-char self-truncation cuts a credential in
  half before the regex runs, leaving an unmatchable prefix on the wire.

So there is no ordering a call site can pick that is safe on its own, and before
this gate every Slack egress path picked one independently: eleven of twelve were
exposed to one hazard or the other, and the one correct pipeline lived in a
private helper in ``dashboard/chat_slack.py`` that nobody else could inherit.

Two tiers, deliberately:

  STRUCTURAL tier -- :func:`test_no_module_converts_slack_markdown_directly`.
  An AST scan asserting that ``slack/format.py`` is the ONLY module in
  ``kiro_crew`` that calls ``to_slack_mrkdwn``. This is what makes alignment a
  property of the code's shape rather than of whether an author remembered:
  a newly added egress path cannot render its own way without failing the build,
  which is precisely the guarantee a per-call-site convention cannot give.

  **Scope, stated so it is not over-read:** this gate polices the CONVERSION
  primitive, not the Slack API calls. A path that posts raw text to Slack without
  ever converting it is NOT covered — it skips redaction and still passes the
  build. Covering that would mean gating ``post_message`` / ``post_blocks``, which
  carry a great deal of legitimately non-agent text (status lines, error notices,
  captions) and so cannot be banned outright the way a conversion call can; it is
  a judgment-tier problem, not a deterministic one. ``security_posture.py`` states
  the same bound, deliberately, rather than claiming every Slack path is enforced.

  BEHAVIOURAL tier -- the ordering tests below. They prove the pipeline the
  structural tier funnels everyone into actually closes both hazards, in both
  the multi-part (:func:`render_for_slack`) and single-message
  (:func:`render_one_for_slack`) forms.

Escape hatch: a genuinely-justified direct call may carry a trailing
``# render-ok: <reason>`` comment. Every suppression must state its reason so the
exception is auditable in review and greppable later.
"""

from __future__ import annotations

import ast
import io
import json
import pathlib
import re
import tokenize

import pytest
from source_corpus import parsed_candidates

from kiro_crew.messaging.display_safety import slack_mrkdwn_reading
from kiro_crew.security import redact_credentials
from kiro_crew.slack import format as slack_format
from kiro_crew.slack.format import (
    CONTINUATION,
    SLACK_MAX_TEXT,
    SLACK_MSG_LIMIT,
    build_options_blocks,
    build_options_selected_blocks,
    ends_inside_code_fence,
    render_for_slack,
    render_one_for_slack,
)

# The conversion primitive nobody outside format.py may call.
_BANNED_FUNC = "to_slack_mrkdwn"
_OWNER_MODULE = "kiro_crew/slack/format.py"

# Trailing-comment marker that suppresses a single flagged call.
_SUPPRESS = "render-ok"

# A representative AWS access key ID: matched by the credential regex, and long
# enough that a straddling cut leaves a recognisable prefix behind.
_SECRET = "AKIAIOSFODNN7EXAMPLE"


def _fake_redactor(text: str) -> str:
    """Stand-in for the platform redactor: exact, injected, no context needed.

    The real ``redact_via_context`` needs a composed PlatformContext, which would
    make these ordering tests depend on platform composition rather than on the
    pipeline. Injecting a redactor keeps the assertions about ORDER.
    """
    return text.replace(_SECRET, "[REDACTED]")


# ── STRUCTURAL tier ───────────────────────────────────────────────────────────


def _src_root() -> pathlib.Path:
    """Locate the kiro_crew source tree (import-first, repo-path fallback)."""
    try:
        import kiro_crew  # noqa: PLC0415

        return pathlib.Path(kiro_crew.__file__).resolve().parent
    except Exception:
        return pathlib.Path(__file__).resolve().parent.parent / "src" / "kiro_crew"


def _aliases(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Resolve how a module might name the banned function.

    Returns ``(bare_names, module_names)``:
      * ``bare_names`` -- locals bound by a ``from ... import to_slack_mrkdwn``,
        including ``as`` renames, so a bare ``tsm(...)`` still resolves.
      * ``module_names`` -- locals bound to the ``slack.format`` MODULE, so
        ``fmt.to_slack_mrkdwn(...)`` resolves too.

    Without both, a single aliased import would silently defeat the gate.
    """
    bare: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith("slack.format"):
                    modules.add(alias.asname or alias.name.split(".")[-1])
        elif isinstance(node, ast.ImportFrom):
            if not (node.module or "").endswith("slack.format"):
                continue
            for alias in node.names:
                if alias.name == _BANNED_FUNC:
                    bare.add(alias.asname or alias.name)
    return bare, modules


def _suppressed_lines(source: str) -> set[int]:
    """Lines carrying a genuine ``# render-ok`` COMMENT, not a substring.

    Tokenizing rather than substring-scanning means a ``render-ok`` inside a
    string literal does NOT suppress, while a real trailing comment does.
    """
    out: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT and _SUPPRESS in tok.string:
                out.add(tok.start[0])
    except (tokenize.TokenError, IndentationError):
        pass
    return out


def find_violations(source: str, path: str = "<source>") -> list[tuple[str, int, str]]:
    """Return ``(path, lineno, name)`` for direct ``to_slack_mrkdwn`` calls."""
    tree = ast.parse(source)
    bare, modules = _aliases(tree)
    suppressed = _suppressed_lines(source)
    out: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name: str | None = None
        if isinstance(func, ast.Name) and func.id in bare:
            name = func.id
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == _BANNED_FUNC
            and isinstance(func.value, ast.Name)
            and func.value.id in modules
        ):
            name = f"{func.value.id}.{func.attr}"
        if name is None:
            continue
        span = range(node.lineno, (node.end_lineno or node.lineno) + 1)
        if any(ln in suppressed for ln in span):
            continue
        out.append((path, node.lineno, name))
    return out


def collect_repo_violations() -> list[tuple[str, int, str]]:
    """Scan every ``kiro_crew/**/*.py`` except the owning module."""
    base = _src_root().parent
    out: list[tuple[str, int, str]] = []
    # A direct call to to_slack_mrkdwn -- bare-imported or reached as
    # ``<module>.to_slack_mrkdwn`` -- can only exist in a file whose TEXT holds the
    # literal ``to_slack_mrkdwn`` (the import that binds the alias, or the attribute
    # call itself), so the shared corpus parses just those files rather than
    # re-walking the whole package for this one gate. Narrowing on the literal
    # drops non-matches only (proved lossless in the PR body).
    for py, text, _tree in parsed_candidates(require_all=(_BANNED_FUNC,)):
        try:
            rel = str(py.relative_to(base))
        except ValueError:  # pragma: no cover - defensive
            rel = str(py)
        if rel.replace("\\", "/").endswith(_OWNER_MODULE):
            continue
        try:
            out.extend(find_violations(text, rel))
        except SyntaxError:  # pragma: no cover - defensive
            continue
    return out


def test_no_module_converts_slack_markdown_directly() -> None:
    """Only slack/format.py may call to_slack_mrkdwn.

    This is the omission detector. A new Slack egress path that renders its own
    way -- and therefore picks one of the two unsafe orderings -- fails here
    instead of shipping a silent credential-disclosure path.
    """
    violations = collect_repo_violations()
    if violations:
        detail = "\n".join(
            f"  {path}:{lineno}  {name}(...)" for path, lineno, name in violations
        )
        raise AssertionError(
            "to_slack_mrkdwn called outside kiro_crew/slack/format.py.\n\n"
            "Neither redact-then-convert nor convert-then-redact is safe on its "
            "own (the ANSI strip reassembles split credentials; the 39,000-char "
            "self-truncation cuts them in half). Use render_for_slack() for a "
            "parts list, or render_one_for_slack() for a single-string sink.\n"
            "If a direct call is genuinely correct, add a trailing "
            "'# render-ok: <reason>' comment.\n\n"
            f"{detail}"
        )


# ── Meta-tests: prove the detector both fires and stays quiet ─────────────────


def test_detector_flags_a_bare_aliased_call() -> None:
    src = (
        "from kiro_crew.slack.format import to_slack_mrkdwn as tsm\n"
        "def f(t):\n"
        "    return tsm(t)\n"
    )
    assert [v[1] for v in find_violations(src)] == [3]


def test_detector_flags_a_module_attribute_call() -> None:
    src = (
        "import kiro_crew.slack.format as fmt\n"
        "def f(t):\n"
        "    return fmt.to_slack_mrkdwn(t)\n"
    )
    assert [v[1] for v in find_violations(src)] == [3]


def test_detector_ignores_same_named_method_on_other_objects() -> None:
    """A method that merely shares the name must not trip the gate."""
    src = "def f(renderer, t):\n    return renderer.to_slack_mrkdwn(t)\n"
    assert find_violations(src) == []


def test_detector_ignores_the_shared_helpers() -> None:
    src = (
        "from kiro_crew.slack.format import render_for_slack\n"
        "def f(t):\n"
        "    return render_for_slack(t)\n"
    )
    assert find_violations(src) == []


def test_render_ok_comment_suppresses() -> None:
    src = (
        "from kiro_crew.slack.format import to_slack_mrkdwn\n"
        "def f(t):\n"
        "    return to_slack_mrkdwn(t)  # render-ok: no egress, formats a local preview\n"
    )
    assert find_violations(src) == []


def test_render_ok_inside_a_string_does_not_suppress() -> None:
    src = (
        "from kiro_crew.slack.format import to_slack_mrkdwn\n"
        "def f():\n"
        '    return to_slack_mrkdwn("render-ok")\n'
    )
    assert [v[1] for v in find_violations(src)] == [3]


# ── BEHAVIOURAL tier: the pipeline closes both hazards ───────────────────────


class TestRenderForSlackOrdering:
    """render_for_slack: the multi-part form."""

    @staticmethod
    def _credential_redactor(text: str) -> str:
        return redact_credentials(text)[0]

    def test_ansi_split_credential_is_not_reassembled(self) -> None:
        """redact-then-convert hazard: the ANSI strip must not rebuild a secret.

        The escape makes the key invisible to the regex, and ``to_slack_mrkdwn``
        removes it -- so a pipeline that redacts before normalising hands Slack
        the whole key. Normalising FIRST is what closes this.
        """
        obfuscated = _SECRET[:4] + "\x1b[0m" + _SECRET[4:]
        body = "\n".join(render_for_slack(f"key is {obfuscated}", redactor=_fake_redactor))
        assert _SECRET not in body
        assert "[REDACTED]" in body

    def test_credential_at_the_conversion_ceiling_is_not_halved(self) -> None:
        """convert-then-redact hazard: the 39k self-truncation must not cut it.

        The filler is newline-free so ``rfind`` cannot snap the cut elsewhere,
        and the secret starts 10 chars before the ceiling -- so convert-first
        leaves exactly its first 10 characters behind. A secret placed wholly
        past the boundary would just be deleted, which is why the obvious
        version of this test proves nothing.
        """
        filler = "x" * (SLACK_MAX_TEXT - 10)
        assert "\n" not in filler
        body = "\n".join(render_for_slack(filler + _SECRET, redactor=_fake_redactor))
        assert _SECRET not in body
        assert _SECRET[:8] not in body, "a truncated credential prefix reached Slack"

    def test_credential_straddling_the_presplit_boundary_is_redacted(self) -> None:
        """The hard case: obfuscated AND cut across two posts.

        Redacting per block after the strip is not equivalent -- each block would
        hold an unmatchable fragment, both would redact clean individually, and
        the two adjacent posts would hand the reader the whole key.
        """
        obfuscated = _SECRET[:6] + "\x1b[0m" + _SECRET[6:]
        cut = SLACK_MAX_TEXT // 2 - len(CONTINUATION)
        filler = "q" * (cut - 12)
        content = filler + obfuscated + ("z" * 200)
        assert len(filler) < cut < len(filler) + len(obfuscated), (
            "the credential must straddle the cut for this test to mean anything"
        )
        body = "".join(render_for_slack(content, redactor=_fake_redactor))
        assert _SECRET not in body
        assert _SECRET[:8] not in body

    def test_tail_past_the_conversion_ceiling_survives(self) -> None:
        """Pre-splitting is what stops conversion silently dropping the tail."""
        marker = "TAILMARKER"
        content = ("y" * (SLACK_MAX_TEXT + 5_000)) + marker
        body = "".join(render_for_slack(content, redactor=_fake_redactor))
        assert marker in body

    def test_every_part_fits_the_limit_including_the_prefix(self) -> None:
        """The prefix is charged against the limit, not bolted on afterwards.

        Decorating a maximally-sized part after the split is what pushed the
        backfill's icon-prefixed messages past SLACK_MSG_LIMIT.
        """
        parts = render_for_slack(
            "z" * (SLACK_MSG_LIMIT * 3), prefix="🤖 ", redactor=_fake_redactor
        )
        assert len(parts) > 1
        assert all(p.startswith("🤖 ") for p in parts)
        assert all(len(p) <= SLACK_MSG_LIMIT for p in parts)

    @pytest.mark.parametrize(
        "key_offset", [3, -10, -25, -60], ids=["before", "inside", "after", "past_the_link"]
    )
    def test_final_native_link_split_cannot_reveal_a_key(self, key_offset: int) -> None:
        """Every posted part and adjacent screen reading stays credential-free.

        The final cut lands *key_offset* characters before the marked key: inside
        the url ahead of it, inside it, after it but inside the label, and past the
        link's closing ``>``. The last is the control: the part holding the whole
        link collapses to its label, and the key is never shown.
        """
        marked_key = "AKIA*IOSFODNN7EXAMPLE*"
        chunk_limit = SLACK_MSG_LIMIT - len(CONTINUATION)
        native_link_prefix = "<https://x/"
        filler_length = chunk_limit + key_offset - len(native_link_prefix)
        payload = native_link_prefix + ("q" * filler_length) + marked_key + "|label>" + ("t" * 200)

        parts = render_for_slack(payload, redactor=self._credential_redactor)

        assert len(parts) == 2
        assert all(len(part) <= SLACK_MSG_LIMIT for part in parts)
        for part in parts:
            assert _SECRET not in part
            assert _SECRET not in slack_mrkdwn_reading(part)
        for left, right in zip(parts, parts[1:]):
            literal_pair = left.strip() + right.strip()
            displayed_pair = (
                slack_mrkdwn_reading(left).strip() + slack_mrkdwn_reading(right).strip()
            )
            assert _SECRET not in literal_pair
            assert _SECRET not in displayed_pair
        if key_offset < -len(marked_key) - len("|label>"):
            converted = slack_format.to_slack_mrkdwn(payload)
            assert parts == slack_format.split_message(converted, limit=SLACK_MSG_LIMIT)

    def test_final_native_link_split_fenced_code_cannot_reveal_a_key(self) -> None:
        marked_key = "AKIA```IOSFODNN7EXAMPLE````"
        payload = "[doc](https://example.com/" + ("q" * 50) + marked_key + ")"
        limit = 60
        unsafe_parts = slack_format.split_message(slack_format.to_slack_mrkdwn(payload), limit)
        assert any(_SECRET in part.replace("```", "") for part in unsafe_parts)

        parts = render_for_slack(payload, limit=limit, redactor=self._credential_redactor)

        assert parts == ["doc"]
        assert all(len(part) <= limit for part in parts)
        assert all(_SECRET not in part.replace("```", "") for part in parts)
        assert all(_SECRET not in slack_mrkdwn_reading(part) for part in parts)

    @pytest.mark.parametrize(
        "mrkdwn", ["`<https://example.com|x>`", "```<https://example.com|x>```"]
    )
    def test_slack_mrkdwn_reading_keeps_native_links_inside_code(self, mrkdwn: str) -> None:
        assert slack_mrkdwn_reading(mrkdwn) == "<https://example.com|x>"

    def test_keyless_final_split_is_byte_identical(self) -> None:
        payload = "<https://x/" + ("q" * (SLACK_MSG_LIMIT + 200)) + "|label>tail"
        converted = slack_format.to_slack_mrkdwn(payload)
        expected = slack_format.split_message(converted, limit=SLACK_MSG_LIMIT)

        assert render_for_slack(payload, redactor=self._credential_redactor) == expected

    def test_final_truncation_of_a_native_link_cannot_reveal_a_key(self) -> None:
        """The collapsed form's overflow cut is settled like a split part.

        The cut keeps the url and the marked key and drops the link's closing
        ``>``, so the kept text is no link and its ``*...*`` reads as bold.
        """
        limit = 400
        payload = "<https://x/" + ("q" * 330) + "AKIA*IOSFODNN7EXAMPLE*|label>" + ("t" * 200)

        rendered = render_one_for_slack(payload, limit=limit, redactor=self._credential_redactor)

        assert "|label>" not in rendered.text
        assert len(rendered.text) <= limit
        assert _SECRET not in rendered.text
        assert _SECRET not in slack_mrkdwn_reading(rendered.text)
        assert rendered.redacted is True

    def test_keyless_final_truncation_is_byte_identical(self) -> None:
        limit = 400
        payload = "<https://x/" + ("q" * 300) + "|label>" + ("t" * 200)
        converted = slack_format.to_slack_mrkdwn(payload)

        rendered = render_one_for_slack(payload, limit=limit, redactor=self._credential_redactor)

        assert rendered.text == slack_format._truncated(converted, limit)[0]
        assert rendered.redacted is False

    def test_a_settle_that_grows_the_cut_text_announces_the_overflow_once(self) -> None:
        """The kept prefix is settled after the cut, and a tag can outgrow the key.

        The re-cut that brings the grown text back under the limit is the same
        overflow cut, so a notice measured on the text it is handed names the
        settled piece's length. One notice, and its total is the reply's.
        """

        def widening_redactor(text: str) -> str:
            return text.replace(_SECRET, "[REDACTED: credential of the example account]")

        limit = 400
        payload = (
            "<https://example.com/" + ("q" * 320) + "AKIA*IOSFODNN7EXAMPLE*|label>" + ("t" * 200)
        )
        converted = slack_format.to_slack_mrkdwn(payload)
        cut_once = slack_format._truncated(converted, limit)[0]
        assert len(cut_once) <= limit
        assert (
            len(slack_format._settled_slack_emission(cut_once, widening_redactor)) > limit
        ), "the settle growing the cut text past the limit is the premise"

        rendered = render_one_for_slack(payload, limit=limit, redactor=widening_redactor)

        assert len(rendered.text) <= limit
        assert rendered.text.count("truncated (") == 1, rendered.text
        assert f"({len(converted)} chars total)" in rendered.text
        assert _SECRET not in slack_mrkdwn_reading(rendered.text)
        assert rendered.redacted is True

    def test_blank_input_yields_no_parts(self) -> None:
        """Callers can post unconditionally: nothing to say means nothing posted."""
        assert render_for_slack("", redactor=_fake_redactor) == []
        assert render_for_slack("   \n\t ", redactor=_fake_redactor) == []

    def test_tables_convert_when_the_message_is_whole(self) -> None:
        table = "| a | b |\n| - | - |\n| 1 | 2 |"
        body = "\n".join(render_for_slack(table, redactor=_fake_redactor))
        assert "*a:*" in body, "a single-block message should get the mobile rendering"

    def test_tables_stay_raw_when_a_split_occurred(self) -> None:
        """A block starting mid-table would adopt a DATA row as its header."""
        rows = "\n".join(f"| r{i} | v{i} |" for i in range(4_000))
        table = f"| a | b |\n| - | - |\n{rows}"
        assert len(table) > SLACK_MAX_TEXT // 2, "this test needs an actual split"
        body = "".join(render_for_slack(table, redactor=_fake_redactor))
        assert "| r3999 | v3999 |" in body, "rows must survive as raw pipes"


class TestConvertedSlackDisplayRedaction:
    """The converted mrkdwn must be safe under Slack's display semantics."""

    _TAIL = _SECRET[4:]
    _REVEALING_SHAPES = [
        ("bold-conversion", f"AKIA**{_TAIL}*"),
        ("italic", f"AKIA_{_TAIL}_"),
        ("strike-conversion", f"AKIA~~{_TAIL}~~"),
        ("inline-code", f"AKIA`{_TAIL}`"),
        ("native-link", f"AK<u|IA>{_TAIL}"),
    ]
    _CONTEXTS = [
        ("plain", "{}"),
        ("refused-link", "[l](https://x/{}|)"),
    ]

    @staticmethod
    def _credential_redactor(text: str) -> str:
        return redact_credentials(text)[0]

    @staticmethod
    def _narrow_generic_screen(monkeypatch: pytest.MonkeyPatch) -> None:
        """Leave the historical generic readings so Slack defenses stay observable."""
        from kiro_crew.messaging import display_safety, split

        further_readings = (display_safety._plain_reading,)
        monkeypatch.setattr(display_safety, "FURTHER_READINGS", further_readings)
        monkeypatch.setattr(
            split,
            "_RENDERINGS",
            (display_safety.canonicalize_display, *further_readings),
        )

    @pytest.mark.parametrize(
        ("shape_name", "shape"),
        _REVEALING_SHAPES,
        ids=[case[0] for case in _REVEALING_SHAPES],
    )
    @pytest.mark.parametrize(
        ("context_name", "context"),
        _CONTEXTS,
        ids=[case[0] for case in _CONTEXTS],
    )
    def test_a_key_revealed_by_converted_mrkdwn_is_redacted(
        self, shape_name: str, shape: str, context_name: str, context: str
    ) -> None:
        payload = context.format(shape)
        rendered = render_one_for_slack(payload, redactor=self._credential_redactor)
        assert _SECRET not in slack_mrkdwn_reading(rendered.text), (
            shape_name,
            context_name,
            rendered.text,
        )
        assert rendered.redacted is True

    def test_the_reported_input_is_safe_in_the_multipart_pipeline(self) -> None:
        second_secret = "ASIA" + self._TAIL
        payload = f"[l](https://x/AKIA**{self._TAIL}*/A``{second_secret[1:]}``|)"
        parts = render_for_slack(payload, redactor=self._credential_redactor)
        assert parts
        joined = "".join(parts)
        for emitted in (*parts, joined):
            shown = slack_mrkdwn_reading(emitted)
            assert _SECRET not in shown
            assert second_secret not in shown
            assert self._credential_redactor(emitted) == emitted
            assert self._credential_redactor(shown) == shown
        single = render_one_for_slack(payload, redactor=self._credential_redactor)
        assert single.redacted is True
        assert _SECRET not in single.text
        assert second_secret not in single.text
        assert _SECRET not in slack_mrkdwn_reading(single.text)
        assert second_secret not in slack_mrkdwn_reading(single.text)

    @pytest.mark.parametrize(
        ("conversion_name", "conversion_shape"),
        [
            ("bold", "AKIA**{tail}*"),
            ("underscore", "AKIA__{tail}__"),
            ("strike", "AKIA~~{tail}~~"),
        ],
    )
    @pytest.mark.parametrize(
        ("display_name", "display_shape"),
        [
            ("inline-code", "ASIA`{tail}`"),
            ("bold", "ASIA*{tail}*"),
            ("italic", "ASIA_{tail}_"),
            ("strike", "ASIA~{tail}~"),
            ("native-link", "A<u|SIA{tail}>"),
        ],
    )
    def test_every_converted_and_displayed_pair_settles(
        self,
        conversion_name: str,
        conversion_shape: str,
        display_name: str,
        display_shape: str,
    ) -> None:
        payload = "[l](https://x/{}/{}|)".format(
            conversion_shape.format(tail=self._TAIL),
            display_shape.format(tail=self._TAIL),
        )
        rendered = render_one_for_slack(payload, redactor=self._credential_redactor)
        assert rendered.redacted is True, (conversion_name, display_name)
        assert slack_mrkdwn_reading(rendered.text) == rendered.text, (
            conversion_name,
            display_name,
            rendered.text,
        )
        assert self._credential_redactor(rendered.text) == rendered.text

    @pytest.mark.parametrize(
        "payload",
        [
            "[l](https://x/AKIA**rest*/ASIA`rest`|)",
            "[l](https://x/AKIA__rest__/ASIA_rest_|)",
            "[l](https://x/AKIA~~rest~~/A<u|SIArest>|)",
        ],
    )
    def test_settling_keeps_keyless_conversion_output_byte_identical(self, payload: str) -> None:
        expected = slack_format.to_slack_mrkdwn(payload)
        assert render_one_for_slack(payload, redactor=self._credential_redactor) == (
            expected,
            False,
        )

    def test_the_rejoined_single_message_is_scanned_as_one_display(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            slack_format,
            "_render_blocks",
            lambda *args, **kwargs: (["AKIA", self._TAIL], False),
        )
        rendered = render_one_for_slack("ignored", redactor=self._credential_redactor)
        assert _SECRET not in slack_mrkdwn_reading(rendered.text)
        assert rendered.redacted is True

    def test_removing_the_settling_pass_reopens_the_reported_leak(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._narrow_generic_screen(monkeypatch)
        second_secret = "ASIA" + self._TAIL
        payload = f"[l](https://x/AKIA**{self._TAIL}*/A``{second_secret[1:]}``|)"
        protected = render_for_slack(payload, redactor=self._credential_redactor)
        assert second_secret not in slack_mrkdwn_reading("".join(protected))

        monkeypatch.setattr(
            slack_format,
            "settled_display_form",
            lambda text, redactor, **kwargs: text,
        )
        unprotected = render_for_slack(payload, redactor=self._credential_redactor)
        assert second_secret in slack_mrkdwn_reading("".join(unprotected))

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ("AKIA**rest*", "AKIA*rest*"),
            ("[l](https://x/AKIA**rest*|)", "[l](https://x/AKIA*rest*|)"),
            ("AKIA__rest_", "AKIA__rest_"),
            ("[l](https://x/AKIA__rest_|)", "[l](https://x/AKIA__rest_|)"),
            ("AKIA_rest_", "AKIA_rest_"),
            ("[l](https://x/AKIA_rest_|)", "[l](https://x/AKIA_rest_|)"),
            ("AKIA~~rest~~", "AKIA~rest~"),
            ("[l](https://x/AKIA~~rest~~|)", "[l](https://x/AKIA~rest~|)"),
            ("AKIA~rest~", "AKIA~rest~"),
            ("[l](https://x/AKIA~rest~|)", "[l](https://x/AKIA~rest~|)"),
            ("AKIA`rest`", "AKIA`rest`"),
            ("[l](https://x/AKIA`rest`|)", "[l](https://x/AKIA`rest`|)"),
            ("AK<u|IA>rest", "AK<u|IA>rest"),
            ("[l](https://x/AK<u|IA>rest|)", "[l](https://x/AK<u|IA>rest|)"),
        ],
    )
    def test_keyless_markup_stays_byte_identical_to_the_head_output(
        self, payload: str, expected: str
    ) -> None:
        rendered = render_one_for_slack(payload, redactor=self._credential_redactor)
        assert rendered == (expected, False)

    def test_disabling_the_converted_display_scan_reopens_the_reported_leak(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._narrow_generic_screen(monkeypatch)
        payload = f"[l](https://x/AKIA**{self._TAIL}*|)"
        protected = render_one_for_slack(payload, redactor=self._credential_redactor)
        assert _SECRET not in slack_mrkdwn_reading(protected.text)
        assert protected.redacted is True

        monkeypatch.setattr(slack_format, "_slack_client_display", lambda text: text)
        unprotected = render_one_for_slack(payload, redactor=self._credential_redactor)
        assert _SECRET in slack_mrkdwn_reading(unprotected.text)
        assert unprotected.redacted is False


class TestLiteralOnlyRedactionKeepsTheBlockMarkup:
    """A converted block dirty only as written keeps its links and emphasis.

    ``_convert_tables`` renders a data row as ``*<header>:* <cell>``, which writes
    the ``:`` a named-credential pattern needs after a ``SecretAccessKey`` header.
    That match lives in the literal bytes alone: Slack's reading of the redacted
    block scans clean, so the block has nothing to settle and must be posted with
    its native links and emphasis intact. Only a key the Slack reading joins costs
    the block its markup.
    """

    _SECRET_VALUE = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    _URL = "https://wiki.example.com/bin/view/Runbook"
    _PAYLOAD = (
        "Findings **so far**:\n\n"
        "| Profile | SecretAccessKey |\n"
        "|---|---|\n"
        f"| prod | {_SECRET_VALUE} |\n\n"
        f"See **runbook** at [the wiki]({_URL}) and _ping_ me.\n"
    )

    @staticmethod
    def _credential_redactor(text: str) -> str:
        return redact_credentials(text)[0]

    def _assert_key_gone_and_markup_kept(self, text: str) -> None:
        assert self._SECRET_VALUE not in text
        assert self._SECRET_VALUE not in slack_mrkdwn_reading(text)
        assert "SecretAccessKey" not in text
        assert f"<{self._URL}|the wiki>" in text
        assert "*so far*" in text
        assert "*runbook*" in text
        assert "_ping_" in text

    def test_the_literal_only_match_is_the_block_as_posted(self) -> None:
        expected = self._credential_redactor(slack_format.to_slack_mrkdwn(self._PAYLOAD))
        parts = render_for_slack(self._PAYLOAD, redactor=self._credential_redactor)
        assert parts == [expected]
        self._assert_key_gone_and_markup_kept(parts[0])

    def test_the_rejoined_single_message_keeps_its_markup(self) -> None:
        rendered = render_one_for_slack(self._PAYLOAD, redactor=self._credential_redactor)
        assert rendered.redacted is True
        self._assert_key_gone_and_markup_kept(rendered.text)

    def test_a_key_the_slack_reading_joins_still_costs_the_markup(self) -> None:
        payload = f"AKIA**{_SECRET[4:]}* and [the wiki]({self._URL})"
        rendered = render_one_for_slack(payload, redactor=self._credential_redactor)
        assert _SECRET not in slack_mrkdwn_reading(rendered.text)
        assert rendered.redacted is True
        assert "<" not in rendered.text

    def test_keyless_table_with_a_link_stays_byte_identical(self) -> None:
        payload = self._PAYLOAD.replace("SecretAccessKey", "Region").replace(
            self._SECRET_VALUE, "eu-west-1"
        )
        assert render_one_for_slack(payload, redactor=self._credential_redactor) == (
            slack_format.to_slack_mrkdwn(payload),
            False,
        )


class TestHeaderCaptionsAreRedactedAtTheSeam:
    """A caption skips conversion, so it needs its own redaction pass.

    Captions like ``⏰ *Cron: <name>*`` are already Slack mrkdwn, so they must not
    go through ``to_slack_mrkdwn`` -- which also means they skip the pipeline's
    redaction. They are usually built from LLM-authored data (a cron's name comes
    from the ``cron_add`` tool), and the hand-rolled ``header + parts[0]`` this
    replaces had already forgotten to redact one.
    """

    def test_a_credential_in_the_caption_is_redacted(self) -> None:
        parts = render_for_slack(
            "body", header=f"⏰ *Cron: {_SECRET}*\n\n", redactor=_fake_redactor
        )
        assert _SECRET not in "".join(parts)
        assert "[REDACTED]" in parts[0]

    def test_the_caption_is_not_converted(self) -> None:
        """Conversion would re-interpret the caption's own *bold*."""
        parts = render_for_slack("body", header="⏰ *Cron: nightly*\n\n", redactor=_fake_redactor)
        assert parts[0].startswith("⏰ *Cron: nightly*")

    def test_the_caption_lands_on_the_first_part_only(self) -> None:
        parts = render_for_slack(
            "z" * (SLACK_MSG_LIMIT * 3), header="CAP\n\n", redactor=_fake_redactor
        )
        assert len(parts) > 1
        assert parts[0].startswith("CAP")
        assert not any(p.startswith("CAP") for p in parts[1:])

    def test_the_caption_is_charged_against_the_limit(self) -> None:
        """Attaching it after splitting is what overflowed the limit before."""
        header = "H" * 200 + "\n\n"
        parts = render_for_slack(
            "z" * (SLACK_MSG_LIMIT * 3),
            limit=SLACK_MSG_LIMIT,
            header=header,
            redactor=_fake_redactor,
        )
        assert all(len(p) <= SLACK_MSG_LIMIT for p in parts), (
            "a caption pushed a part past the limit"
        )

    def test_an_empty_body_still_yields_the_caption(self) -> None:
        """The cron path attaches an ack button to the first part."""
        assert render_for_slack("", header="CAP", redactor=_fake_redactor) == ["CAP"]

    def test_no_caption_still_yields_nothing_for_empty_text(self) -> None:
        assert render_for_slack("", redactor=_fake_redactor) == []


class TestFencedCodeSurvivesThePreSplit:
    """A ``` block longer than the pre-split window must not be rewritten.

    Fence state is per-conversion-call. Convert in blocks without carrying it and
    the block that opens mid-fence is treated as prose, so ``_convert_inline``
    rewrites the CODE -- turning `**x**` inside a code sample into Slack bold, and
    mangling anything markdown-shaped the user is quoting verbatim. A fenced region
    can be longer than any block size, so no choice of cut point avoids this.
    """

    def _long_fence(self) -> str:
        """A fenced block that is guaranteed to straddle the pre-split."""
        marker = "**not_bold_in_code**"
        filler = "\n".join(f"line {i} {marker}" for i in range(1_200))
        assert len(filler) > SLACK_MAX_TEXT // 2, "the fence must straddle a block boundary"
        return f"```python\n{filler}\n```"

    def test_code_inside_a_straddling_fence_is_not_converted(self) -> None:
        out = render_one_for_slack(
            self._long_fence(), keep_tables=True, redactor=_fake_redactor
        ).text
        assert "truncated" not in out, "this fixture must fit in one message"
        assert "**not_bold_in_code**" in out, "inline conversion rewrote fenced code"
        assert "*not_bold_in_code*" not in out.replace("**not_bold_in_code**", "")

    def test_the_multipart_form_carries_fence_state_too(self) -> None:
        """Counted, not merely present.

        The FIRST block opens the fence itself, so it converts correctly with or
        without the carry -- an `in body` assertion is satisfied by block 1 alone
        and passes against the bug. Counting every line proves the LATER blocks
        were also treated as code.
        """
        body = "".join(render_for_slack(self._long_fence(), redactor=_fake_redactor))
        assert body.count("**not_bold_in_code**") == 1_200, (
            "later blocks lost fence state and had their code rewritten"
        )

    def test_the_fence_tracker_agrees_with_the_converter(self) -> None:
        assert ends_inside_code_fence("```py\nx = 1") is True
        assert ends_inside_code_fence("```py\nx = 1\n```") is False
        assert ends_inside_code_fence("plain text") is False
        # Carries a start state in, so blocks can be chained.
        assert ends_inside_code_fence("still in code", start=True) is True
        assert ends_inside_code_fence("```", start=True) is False


class TestRenderOneForSlackOrdering:
    """render_one_for_slack: the collapsed form used by the streaming sinks."""

    def test_ansi_split_credential_is_not_reassembled(self) -> None:
        obfuscated = _SECRET[:4] + "\x1b[0m" + _SECRET[4:]
        out = render_one_for_slack(f"key is {obfuscated}", redactor=_fake_redactor).text
        assert _SECRET not in out
        assert "[REDACTED]" in out

    def test_credential_at_the_conversion_ceiling_is_not_halved(self) -> None:
        filler = "x" * (SLACK_MAX_TEXT - 10)
        out = render_one_for_slack(filler + _SECRET, redactor=_fake_redactor).text
        assert _SECRET not in out
        assert _SECRET[:8] not in out

    def test_it_reports_that_redaction_fired(self) -> None:
        """The streaming caller REPLACES an already-posted message on this flag.

        The answer is streamed incrementally by a per-chunk redactor that sees
        raw chunks and does not strip ANSI, so it can miss a credential that only
        becomes matchable after normalisation. Nothing downstream can recover the
        signal either -- the post-render scan sees text this call already cleaned.
        So if this flag were not reported, the unredacted stream would stay
        visible on screen.
        """
        obfuscated = _SECRET[:4] + "\x1b[0m" + _SECRET[4:]
        assert render_one_for_slack(f"key {obfuscated}", redactor=_fake_redactor).redacted

    def test_it_reports_no_redaction_for_clean_text(self) -> None:
        """Otherwise the flag would fire every turn and the overwrite is pointless."""
        assert not render_one_for_slack("just an ordinary answer", redactor=_fake_redactor).redacted

    def test_stripping_ansi_alone_is_not_reported_as_redaction(self) -> None:
        """Normalisation is not a disclosure that was caught."""
        assert not render_one_for_slack("plain \x1b[0m text", redactor=_fake_redactor).redacted

    def test_overflow_is_announced_not_silently_dropped(self) -> None:
        out = render_one_for_slack("y" * (SLACK_MAX_TEXT * 2), redactor=_fake_redactor).text
        assert len(out) <= SLACK_MAX_TEXT
        assert "truncated" in out, "a single-message sink must SAY it lost the tail"

    def test_keep_tables_can_be_forced_by_the_caller(self) -> None:
        """The streaming sink renders tables itself, so it forces raw pipes."""
        table = "| a | b |\n| - | - |\n| 1 | 2 |"
        assert "| a | b |" in render_one_for_slack(
            table, keep_tables=True, redactor=_fake_redactor
        ).text
        assert (
            "*a:*"
            in render_one_for_slack(table, keep_tables=False, redactor=_fake_redactor).text
        )

    def test_keep_tables_can_only_add_rawness_never_remove_it(self) -> None:
        """An explicit False must NOT defeat the straddle guard.

        The streaming caller passes ``keep_tables=actually_streamed``, which is
        False on every turn where start_stream returned None. If False forced
        conversion, each pre-split block would convert independently: a block
        beginning on a table DATA row makes _convert_tables adopt that row as
        headers, and _flush_table drops it entirely when no rows follow. So the
        flag is advisory in one direction only.
        """
        rows = "\n".join(f"| r{i} | v{i} |" for i in range(1_500))
        table = f"| a | b |\n| - | - |\n{rows}"
        # Sized deliberately between the two ceilings: past the pre-split
        # boundary (so more than one block is converted) but under the
        # single-message limit (so nothing is lost to truncation instead, which
        # would make the assertion below prove the wrong thing).
        assert SLACK_MAX_TEXT // 2 < len(table) < SLACK_MAX_TEXT
        out = render_one_for_slack(table, keep_tables=False, redactor=_fake_redactor).text
        assert "truncated" not in out, "the fixture outgrew the single-message limit"
        assert "| r1499 | v1499 |" in out, "an explicit False lost table rows to the split"
        assert "*a:*" not in out, "tables were converted across a split boundary"

    def test_a_long_unbroken_line_gains_no_invented_newline(self) -> None:
        """The internal pre-split must be invisible in the result.

        A response with no newline in it at all still exceeds the pre-split
        window, so it is cut mid-line. Rejoining those blocks with "\\n" would
        insert a line break the model never wrote -- corrupting the text at
        character 19,484 of a 19,501-character response.
        """
        text = "z" * (SLACK_MAX_TEXT // 2 + 1)
        assert "\n" not in text
        out = render_one_for_slack(text, redactor=_fake_redactor).text
        assert "truncated" not in out, "this fixture must fit in one message"
        assert "\n" not in out, "the pre-split invented a newline"
        assert out == text, "the pre-split was not lossless"

    def test_newlines_at_a_block_boundary_survive_exactly(self) -> None:
        """The converse: a boundary newline must not be swallowed either.

        split_message lstrips newlines at its cut, so joining ITS blocks with ""
        would silently delete one. The internal splitter keeps the newline on the
        left block instead, so both directions hold.
        """
        line = "a" * 100
        text = "\n".join([line] * (SLACK_MAX_TEXT // 2 // 101 + 60))
        assert len(text) > SLACK_MAX_TEXT // 2, "must pre-split"
        out = render_one_for_slack(text, keep_tables=True, redactor=_fake_redactor).text
        assert "truncated" not in out, "this fixture must fit in one message"
        assert out.count("\n") == text.count("\n"), "a boundary newline was lost or added"

    def test_internal_split_markers_do_not_reach_the_message(self) -> None:
        """The pre-split is internal; its CONTINUATION must not survive the join.

        Blocks are joined back into ONE message here, so a leftover marker renders
        a visible "(continued…)" where nothing was continued.
        """
        text = "y" * (SLACK_MAX_TEXT // 2 + 5_000)
        out = render_one_for_slack(text, redactor=_fake_redactor).text
        assert CONTINUATION.strip() not in out
        assert "continued" not in out

    def test_a_response_just_under_the_limit_keeps_its_tail(self) -> None:
        """Marker padding must not tip a fits-exactly response into truncation.

        The pre-split adds a CONTINUATION per block. Joined, that padding pushes a
        38,990-char response past 39,000, and the tail is then replaced by the
        truncation notice -- losing valid content to an internal bookkeeping
        artefact rather than to the real size limit.
        """
        marker = "TAILMARKER"
        text = ("z" * (SLACK_MAX_TEXT - 10 - len(marker))) + marker
        assert len(text) < SLACK_MAX_TEXT
        assert len(text) > SLACK_MAX_TEXT // 2, "must pre-split for this to mean anything"
        out = render_one_for_slack(text, redactor=_fake_redactor).text
        assert "truncated" not in out, "a response that fits was truncated anyway"
        assert marker in out, "the tail was lost"

    def test_blank_input_yields_empty_string(self) -> None:
        assert render_one_for_slack("", redactor=_fake_redactor).text == ""
        assert render_one_for_slack("  \n ", redactor=_fake_redactor).text == ""


class TestOptionsChoicesAreRedacted:
    """OPTIONS choice labels are Slack egress too, and Block Kit covers nobody.

    The tag is extracted from RAW text on purpose -- conversion self-truncates at
    39,000 characters and would eat the tag -- so the choices arrive unscanned and
    must be redacted at the sink that renders them.
    """

    def test_checkbox_labels_and_values_are_redacted(self) -> None:
        blocks = build_options_blocks(
            [f"Retry with {_SECRET}", "Abort"], redactor=_fake_redactor
        )
        payload = json.dumps(blocks)
        assert _SECRET not in payload
        assert "[REDACTED]" in payload

    def test_the_button_value_is_redacted_before_it_is_sliced(self) -> None:
        """The value is echoed back into the session on submit.

        ``value`` is ``choice[:150]``. Slicing first can cut a credential into a
        prefix the regex does not match, so redaction has to precede the
        slice -- the same ordering hazard as the conversion ceiling.
        """
        pad = "p" * 140
        blocks = build_options_blocks([pad + _SECRET], redactor=_fake_redactor)
        value = blocks[0]["elements"][0]["options"][0]["value"]
        assert _SECRET[:8] not in value, "a credential fragment survived into the value"

    def test_an_ansi_split_credential_in_a_choice_is_redacted(self) -> None:
        obfuscated = _SECRET[:4] + "\x1b[0m" + _SECRET[4:]
        payload = json.dumps(build_options_blocks([obfuscated], redactor=_fake_redactor))
        assert _SECRET not in payload

    def test_the_selected_view_is_redacted_too(self) -> None:
        payload = json.dumps(
            build_options_selected_blocks(
                [f"Retry with {_SECRET}", "Abort"], [0], redactor=_fake_redactor
            )
        )
        assert _SECRET not in payload

    def test_ordinary_choices_are_unchanged(self) -> None:
        """Redaction must be identity for text that holds no secret."""
        blocks = build_options_blocks(["Merge it now", "Show me the diff"])
        labels = [o["text"]["text"] for o in blocks[0]["elements"][0]["options"]]
        assert labels == ["Merge it now", "Show me the diff"]


def test_default_redactor_is_the_platform_shim() -> None:
    """Left to itself, the pipeline must use the fail-closed context redactor.

    An injectable redactor is only safe if the DEFAULT is the canonical one --
    otherwise a caller that omits it silently gets no redaction at all.
    """
    import kiro_crew.slack.format as fmt

    src = pathlib.Path(fmt.__file__).read_text(encoding="utf-8")
    assert src.count("redact_via_context") >= 2, (
        "both render helpers must default to redact_via_context"
    )


@pytest.mark.parametrize("limit", [1, 5, len(CONTINUATION), len(CONTINUATION) + 1])
def test_pathological_limits_terminate(limit: int) -> None:
    """A limit at or below the continuation marker must not spin forever."""
    parts = render_for_slack("a\nb\nc\n" * 50, limit=limit, redactor=_fake_redactor)
    assert parts


class TestEmphasisDelimiterRedaction:
    """Emphasis delimiters must not smuggle a credential past redaction.

    Same shape as the ANSI vector this module exists for: a transformation applied
    AFTER the scan reassembles what the scanner saw as broken. Here the
    transformation is Slack's own renderer, which consumes ``*`` / ``_`` / ``~`` /
    backticks and shows the reader the joined text.
    """

    KEY = "AKIAIOSFODNN7EXAMPLE"

    def _reader_sees(self, wire: str) -> str:
        """Approximate what Slack DISPLAYS: links show their label, delimiters go."""
        out = re.sub(r"\[([^\]\n]*)\]\([^)\n]*\)", r"\1", wire)
        out = re.sub(r"<(?:[^>|\n]*)\|([^>\n]*)>", r"\1", out)
        for delim in ("**", "*", "__", "_", "~~", "~", "`"):
            out = out.replace(delim, "")
        return out

    @pytest.mark.parametrize(
        "payload",
        [
            "AKIA**IOSFODNN7EXAMPLE**",
            "AKIAIOSF**ODNN**7EXAMPLE",
            "AKIAIOSF_ODNN_7EXAMPLE",
            "AKIAIOSF~~ODNN~~7EXAMPLE",
            "AKIAIOSF`ODNN`7EXAMPLE",
        ],
    )
    def test_delimiter_split_key_never_reaches_a_reader(self, payload):
        rendered = render_one_for_slack(f"credential: {payload}")
        assert self.KEY not in self._reader_sees(rendered.text)
        # The flag is load-bearing: the streaming path uses it to overwrite text it
        # already published, so a silent redaction would leave the key on screen.
        assert rendered.redacted is True

    def test_plain_key_still_redacted(self):
        """Control -- keeps the suite honest if the redactor stops firing at all."""
        rendered = render_one_for_slack(f"credential: {self.KEY}")
        assert self.KEY not in rendered.text
        assert rendered.redacted is True

    def test_ordinary_emphasis_survives_when_nothing_is_secret(self):
        """The downgrade is one-directional: no credential, no formatting loss."""
        rendered = render_one_for_slack("this is **bold** and _italic_ text")
        assert "*bold*" in rendered.text
        assert "_italic_" in rendered.text
        assert rendered.redacted is False

    def test_header_is_canonicalised_too(self):
        """A caption is redacted but NOT converted, so it needs the same guard."""
        parts = render_for_slack("body", header="key AKIA**IOSFODNN7EXAMPLE** ")
        assert self.KEY not in self._reader_sees("".join(parts))

    @pytest.mark.parametrize(
        "payload",
        [
            "[AKIA](https://example.com)IOSFODNN7EXAMPLE",
            "AKIAIOSF[ODNN](https://example.com)7EXAMPLE",
            "<https://example.com|AKIA>IOSFODNN7EXAMPLE",
            # A balanced pair in the url: Slack links the whole destination, so
            # the screen has to collapse the same link or the halves join unseen.
            "[AKIA](https://example.com/a_(b))IOSFODNN7EXAMPLE",
        ],
    )
    def test_link_markup_cannot_smuggle_a_key(self, payload):
        """A link DISPLAYS only its label, so the url is an invisible splitter."""
        rendered = render_one_for_slack(f"credential: {payload}")
        assert self.KEY not in self._reader_sees(rendered.text)
        assert rendered.redacted is True

    def test_ordinary_link_survives(self):
        """Links are only collapsed for the SCAN -- real links must still work."""
        rendered = render_one_for_slack("see [docs](https://example.com) please")
        # Exact form, not a substring check: asserting a host appears *somewhere*
        # would also pass if the url had been mangled into the wrong position.
        assert rendered.text == "see <https://example.com|docs> please"
        assert rendered.redacted is False

    def test_ordinary_parenthesised_link_survives(self):
        """Collapsing a balanced-pair link for the scan must not disturb its render."""
        url = "https://en.wikipedia.org/wiki/Python_(programming_language)"
        rendered = render_one_for_slack(f"see [Python]({url}) please")
        assert rendered.text == f"see <{url}|Python> please"
        assert rendered.redacted is False

    @pytest.mark.parametrize(
        "choice",
        [
            "use AKIAIOSF`ODNN`7EXAMPLE",
            "use AKIA**IOSFODNN7EXAMPLE**",
            "use [AKIA](https://example.com)IOSFODNN7EXAMPLE",
        ],
    )
    def test_options_choices_get_the_display_guard(self, choice):
        """A selected choice is echoed back as mrkdwn, so it has the same vector."""
        blocks = json.dumps(build_options_blocks([choice]))
        assert self.KEY not in self._reader_sees(blocks)

    @pytest.mark.parametrize(
        "pattern_name, required",
        [
            # Each negated class must exclude the delimiters that bound it: the
            # Markdown label and both nested-opener labels sit in [...], while the
            # URL character, escapee and balanced-group character sit in (...).
            (
                "_MD_LINK",
                [
                    ("\\[", "\\]"),
                    ("(", ")"),
                    ("\\[", "\\]"),
                    ("(", ")"),
                    ("(", ")"),
                    ("\\[", "\\]"),
                    ("(", ")"),
                ],
            ),
            ("_SLACK_LINK", [("<", ">"), ("<", ">")]),
        ],
    )
    def test_link_patterns_exclude_their_own_delimiters(self, pattern_name, required):
        """Structural guard against re-introducing polynomial backtracking.

        Asserted on the pattern source rather than by timing, deliberately: a
        wall-clock ratio test for this flakes on a loaded runner (this repo already
        carries one such test that has to be ignored), while the property that
        actually prevents the blow-up is static.

        If a negated class admits a delimiter that bounds it, input like
        ``[[[[[[...`` makes every start position consume the rest of the string
        before failing, which is quadratic in attacker-supplied text on the egress
        path (CodeQL ``py/polynomial-redos``). Excluding those delimiters makes a
        doomed start fail in constant time.

        The patterns live in ``messaging.display_safety`` rather than here: the
        hazard is not Slack-specific (Telegram and Discord collapse the same
        syntax), and the shared overflow sink needs the canonicaliser too.
        """
        from kiro_crew.messaging import display_safety as fmt

        classes = re.findall(r"\[\^((?:\\.|[^\]\\])*)\]", getattr(fmt, pattern_name).pattern)
        assert len(classes) == len(required), (
            f"{pattern_name} has {len(classes)} negated classes, expected "
            f"{len(required)} -- update this test alongside the pattern"
        )
        for cls, delims in zip(classes, required):
            for delim in delims:
                assert delim in cls, (
                    f"{pattern_name} negated class [^{cls}] admits {delim!r}, "
                    f"which reintroduces polynomial backtracking"
                )
