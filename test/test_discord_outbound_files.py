from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_discord import FakeClient

from kiro_crew.discord.client import (
    DISCORD_MAX_FILE_BYTES,
    DISCORD_MAX_FILES_PER_MESSAGE,
    DISCORD_MAX_TEXT,
    DISCORD_MAX_TOTAL_UPLOAD_BYTES,
    _build_upload_form,
    upload_filename,
)
from kiro_crew.discord.renderer import _UPLOAD_LIMITS, DiscordRenderer
from kiro_crew.discord.transport import DISCORD_CAPABILITIES, DiscordTransport
from kiro_crew.messaging import outbound_files as outbound
from kiro_crew.messaging.display_safety import canonicalize_display

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_JPEG = b"\xff\xd8\xff" + b"\x11" * 64
_KEY = "AKIAIOSFODNN7EXAMPLE"
_TEST_UPLOAD_ROOT = ""


@pytest.fixture(scope="module", autouse=True)
def _set_test_upload_root(tmp_path_factory: Any) -> None:
    global _TEST_UPLOAD_ROOT
    _TEST_UPLOAD_ROOT = str(tmp_path_factory.getbasetemp())


def _png(tmp_path: Path, name: str = "chart.png", size: int = 0) -> Path:
    path = tmp_path / name
    path.write_bytes(_PNG + b"\x00" * max(0, size - len(_PNG)))
    return path


def _file(path: str = "/tmp/a.png", *, data: bytes = _PNG, alt: str = "", mime: str = "") -> Any:
    return outbound.OutboundFile(path=path, data=data, alt=alt, mime=mime or "image/png")


def _renderer(**kw: Any) -> tuple[DiscordRenderer, FakeClient]:
    cli, caps = FakeClient(), kw.pop("capabilities", None) or DISCORD_CAPABILITIES
    root = kw.pop("upload_root", _TEST_UPLOAD_ROOT)
    # Explicit opt-in: the renderer defaults to deny, so upload-exercising
    # tests must pass the gate like production does (tests for the denied
    # path pass uploads_allowed=False explicitly).
    kw.setdefault("uploads_allowed", True)
    r = DiscordRenderer(cli, "chan1", caps, session_key="discord:u1", upload_root=root, **kw)  # type: ignore[arg-type]
    return r, cli


async def _turn(body: str | tuple[str, ...], **kw: Any) -> FakeClient:
    r, cli = _renderer(**kw)
    for chunk in (body,) if isinstance(body, str) else body:
        await r.on_text_chunk(chunk)
    await r.on_done()
    return cli


def _fields(payload: dict, files: list, **kw: Any) -> list[tuple[dict, Any, Any]]:
    return [(o, h, v) for (o, h, v) in _build_upload_form(payload, files, **kw)._fields]


def _doc(name: str = "report.pdf", *, data: bytes = b"%PDF-1.4 body") -> Any:
    return outbound.OutboundFile(
        path=f"/tmp/outbox/{name}", data=data, alt="", mime="application/pdf"
    )


def _client(monkeypatch: Any, reply: dict) -> tuple[Any, list[dict]]:
    """A DiscordClient whose session records the multipart body it was handed."""
    from contextlib import asynccontextmanager

    from kiro_crew.discord.client import DiscordClient

    seen: list[dict] = []

    @asynccontextmanager
    async def request(method: str, url: str, **kw: Any) -> Any:
        seen.append({k: v for k, v in kw.items() if k == "data"})

        async def response_json(content_type: Any = None) -> Any:
            return reply

        yield SimpleNamespace(status=200, json=response_json)

    client = DiscordClient(token="t")
    monkeypatch.setattr(client, "_ensure_session", lambda: _done(SimpleNamespace(request=request)))
    return client, seen


def _description(alt: str) -> str:
    return json.loads(_fields({}, [_file(alt=alt)])[0][2])["attachments"][0].get("description", "")


def _bodies(cli: FakeClient) -> list[str]:
    return [t for t, _ in cli.sent] + [t for _, t, _ in cli.edits]


def _slot(restricted: bool) -> Any:
    return type("Slot", (), {"is_restricted": restricted})()


# fmt: off
def _restricted(key: str, slot: Any = None) -> bool:
    """Drive Discord's gate with a fake dashboard state holding *slot*.

    Injects the STATE rather than stubbing the slot lookup, so the real
    ``get_slot`` path in ``messaging/upload_gate.py`` is what answers.
    """
    from kiro_crew.discord.transport_dispatch import DiscordDispatcher

    state = SimpleNamespace(get_slot=lambda _name: slot)
    dispatcher = SimpleNamespace(_session_resume=SimpleNamespace(dashboard_state=state))
    return asyncio.run(DiscordDispatcher._uploads_restricted(dispatcher, key))


def _stage_sessions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **modes: str) -> None:
    from kiro_crew.dashboard.handlers import _shared

    sessions = tmp_path / "sessions"
    sessions.mkdir(exist_ok=True)
    for stem, mode in modes.items():
        (sessions / f"{stem}.jsonl").write_text('{"_type": "metadata", "memory_mode": "%s"}\n' % mode, encoding="utf-8")
    monkeypatch.setattr(_shared, "config_dir", lambda: tmp_path)


def _shrink_file_cap(monkeypatch: pytest.MonkeyPatch, cap: int = 64) -> None:
    monkeypatch.setattr("kiro_crew.discord.renderer._UPLOAD_LIMITS", replace(_UPLOAD_LIMITS, max_file_bytes=cap))


class TestSealTimeExtraction:
    @pytest.mark.asyncio
    async def test_one_upload_per_sealed_segment_with_bytes_not_path(self, tmp_path: Path) -> None:
        cli = await _turn(f"    ![literal]({(path := _png(tmp_path))})\n\nHere is the chart.\n\n![Revenue]({path})\n\nDone.")
        assert [verb for verb, _ in cli.uploads] == ["edit"] and len(cli.uploaded_files) == 1 and (sent := cli.uploaded_files[0]).data == path.read_bytes() and (sent.mime, sent.alt) == ("image/png", "Revenue"), f"expected ONE upload per seal: {cli.final_text()!r}"
        assert (final := cli.final_text()).count(str(path)) == 1 and "![literal]" in final and "![Revenue]" not in final and "Here is the chart." in final and "Done." in final

    @pytest.mark.parametrize("arrived", [True, False])
    @pytest.mark.asyncio
    async def test_live_frames_never_show_image_markup(self, tmp_path: Path, arrived: bool) -> None:
        path, (r, cli) = _png(tmp_path), _renderer()
        await r.on_text_chunk(f"Building it.\n\n![Revenue]({path}" + (")" if arrived else ""))
        await r.on_tool_call("t1", "shell")  # forces a frame past the 1.2s throttle
        await r.on_text_chunk(("" if arrived else ")") + "\n\nDone.")
        await r.on_done()
        live = [t for t, _ in cli.sent] + [t for _, t, _ in cli.edits[:-1]]
        assert live and all(str(path) not in frame and "![" not in frame for frame in live)

    @pytest.mark.asyncio
    async def test_image_removal_cannot_reassemble_a_credential(self, tmp_path: Path) -> None:
        bodies = (f"{_KEY[:4]}![x]({(path := _png(tmp_path))}){_KEY[4:]}", f"[{_KEY[:4]}](![x]({path})https://example.com){_KEY[4:]}")
        for body in bodies:
            cli = await _turn(body)
            assert cli.uploaded_files and all(_KEY not in canonicalize_display(text) for text in _bodies(cli))

    @pytest.mark.asyncio
    async def test_image_removal_cannot_reassemble_a_mention(self, tmp_path: Path) -> None:
        mention, literal = "<@123456789012345678>", "@dataclass @media @scope/pkg a@b.com"
        for dest in ("/missing.png", str(_png(tmp_path))):
            bodies = _bodies(cli := await _turn(f"{literal} <@123456789![x]({dest})012345678>"))
            assert bodies and all(mention not in text for text in bodies) and literal in cli.final_text()

    @pytest.mark.asyncio
    async def test_mention_neutralization_cannot_exceed_transport_limit(self) -> None:
        source, (r, cli) = ("@here " * 300).strip(), _renderer()
        r._buf = [source]
        await r._seal_current()
        bodies = [text for text, _components in cli.sent]
        assert all(len(body) <= DISCORD_MAX_TEXT for body in bodies)
        assert "".join(bodies).replace("@\u200bhere", "@here") == source

    @pytest.mark.asyncio
    async def test_each_rotated_segment_uploads_only_its_own_file(self, tmp_path: Path) -> None:
        (first, second), (r, cli) = (_png(tmp_path, "one.png"), _png(tmp_path, "two.png")), _renderer()
        await r.on_text_chunk(f"First.\n\n![one]({first})\n")
        await r.on_steer_consumed("go on")
        await r.on_text_chunk(f"Second.\n\n![two]({second})\n")
        await r.on_done()
        assert (names := [Path(f.path).name for _v, files in cli.uploads for f in files]) == ["one.png", "two.png"] and [len(files) for _v, files in cli.uploads] == [1, 1], f"a file uploaded twice, or leaked segments: {names}"

    @pytest.mark.parametrize("fence,inner", [("~~~", "`\n"), ("````", "```\n")])
    @pytest.mark.asyncio
    async def test_fence_survives_rotation(self, tmp_path: Path, fence: str, inner: str) -> None:
        (literal, outside), (r, cli) = (_png(tmp_path, "literal.png"), _png(tmp_path, "outside.png")), _renderer()
        markup = f"![outside]({outside})"
        body = f"{fence}{'x' * 300 if fence == '~~~' else 'md'}\n" + "x" * (r._limit() + 20) + f"\n{inner}![literal]({literal})\n{fence}\n" + "y" * (r._limit() - len(markup) // 2) + markup + " tail"
        await r.on_text_chunk(body)
        await r.on_done()
        assert [Path(file.path).name for file in cli.uploaded_files] == ["outside.png"]
        assert any(str(literal) in text for text in _bodies(cli))
        assert all(str(outside) not in t and len(t) <= DISCORD_MAX_TEXT for t in _bodies(cli))

    @pytest.mark.asyncio
    async def test_raw_fence_degradation_keeps_literal_image_markup(self, tmp_path: Path) -> None:
        (fence, path), (r, cli) = ("`" * 500, _png(tmp_path)), _renderer()
        await r.on_text_chunk(f"{fence}\n{'x' * r._limit()}\n![literal]({path})\n{fence}")
        await r.on_done()
        assert cli.uploaded_files == [] and any(f"![literal]({path})" in text for text in _bodies(cli))

    @pytest.mark.asyncio
    async def test_a_failed_edit_falls_through_to_a_send_with_the_same_bytes(self, tmp_path: Path) -> None:
        path, (r, cli) = _png(tmp_path), _renderer()
        await r.on_text_chunk(f"![Revenue]({path})\n\nThere it is.")
        cli.edit_ok = False  # live message deleted mid-turn
        await r.on_done()
        assert [verb for verb, _ in cli.uploads] == ["edit", "send"] and len({id(f) for f in cli.uploaded_files}) == 1, "the fallthrough re-read the file instead of reusing bytes"

    @pytest.mark.parametrize("case", ["fenced", "sensitive", "restricted", "no_capability", "oversize", "near_cap"])
    @pytest.mark.asyncio
    async def test_markup_stays_when_nothing_can_be_uploaded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
        from kiro_crew.discord import renderer as renderer_module

        events: list[dict] = []
        monkeypatch.setattr(renderer_module, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw)))
        big = case in ("oversize", "near_cap")
        _shrink_file_cap(monkeypatch) if big else None
        name = f"{_KEY[:4]}\u200b{_KEY[4:]}.png" if case == "near_cap" else "chart.png"
        path = tmp_path / "nope.png" if case == "sensitive" else _png(tmp_path, name=name, size=4096 if big else 0)
        markup, body = (m := f"![Revenue]({path})"), (f"```md\n{m}\n```\n" if case == "fenced" else f"Chart:\n\n{m}\n")
        caps = replace(DISCORD_CAPABILITIES, files_outbound=False) if case == "no_capability" else None
        root = str(tmp_path / "approved") if case == "sensitive" else str(tmp_path)
        prefix = "y" * 1700 + f"\n\n{_KEY[:4]}**{_KEY[4:]}**\n\n" if case == "near_cap" else ""
        cli = await _turn(prefix + body, uploads_allowed=case != "restricted", capabilities=caps, upload_root=root)
        assert cli.uploads == [] and "![Revenue](" in (final := cli.final_text()) and (case == "near_cap" or markup in final), "a refused file must retain its markup and never reach the wire"
        reason = {"sensitive": "sensitive", "oversize": "over_file_bytes", "near_cap": "over_file_bytes"}.get(case)
        assert [event["error"] for event in events] == ([reason] if reason else [])
        assert all(event["outcome"] == "denied" and str(path) not in str(event) for event in events)
        assert case != "oversize" or "per-file limit" in final, "the refusal must be stated"
        assert case != "near_cap" or (_KEY not in canonicalize_display(final) and len(final) <= DISCORD_CAPABILITIES.max_message_chars)

    @pytest.mark.asyncio
    async def test_image_only_reply_sends_file_without_raw_markup(self, tmp_path: Path) -> None:
        cli = await _turn(f"![Revenue]({_png(tmp_path)})")
        assert len(cli.uploaded_files) == 1 and all(str(tmp_path / "chart.png") not in body and "![Revenue]" not in body for body in _bodies(cli))

    @pytest.mark.parametrize("mode", ["fail_uploads", "raise_uploads"])
    @pytest.mark.asyncio
    async def test_a_failed_upload_recovers_safely(self, tmp_path: Path, mode: str) -> None:
        for hidden in (False, True):
            path, (r, cli) = _png(tmp_path), _renderer()
            cli.edit_ok, _ = False, setattr(cli, mode, True)
            await r.on_text_chunk((f"{_KEY[:4]}**{_KEY[4:]}**\n" if hidden else "Here it is.\n\n") + f"![Revenue]({path})")
            await r.on_done()
            assert cli.uploads, "the upload was never attempted"
            assert (recovered := [t for t, _ in cli.sent]) and all(_KEY not in canonicalize_display(t) for t in recovered)
            assert hidden or any(f"![Revenue]({path})" in t for t in recovered), f"markup was not restored: {recovered!r}"

    @pytest.mark.asyncio
    async def test_the_recovery_post_keeps_every_authored_character(self, tmp_path: Path) -> None:
        path, (r, cli) = _png(tmp_path), _renderer()
        r._buf, cli.edit_ok, cli.fail_uploads = [body := "```\n" + "z" * (r._limit() - len(markup := f"![Revenue]({path})") - 14) + "\n```\n\nTAIL_MARKER " + markup], False, True
        assert r._limit() < len(body) <= DISCORD_MAX_TEXT, "premise: over _limit(), under the cap"
        await r._seal_current()
        assert (landed := "".join(t for t, _ in cli.sent)) and "TAIL_MARKER" in landed and markup in landed and all(len(t) <= DISCORD_MAX_TEXT for t, _ in cli.sent), f"authored content lost: {landed[-120:]!r}"

    @pytest.mark.asyncio
    async def test_a_failed_middle_send_does_not_drop_later_chunks(self) -> None:
        from unittest.mock import AsyncMock
        r, _ = _renderer()
        r._buf, components = ["A" * (DISCORD_MAX_TEXT * 2 + 1)], [{"type": 1}]
        r._land_sealed = land = AsyncMock(side_effect=[True, False, True])  # type: ignore[method-assign]
        await r._seal_current(components=components, extract_uploads=False)
        assert land.await_count == 3 and land.await_args_list[-1].args[2] is components


class TestRestrictedGate:
    @pytest.mark.parametrize("key,slot,expected", [("discord:u1:", None, False), ("dashboard:abc", _slot(False), False), ("dashboard:abc", _slot(True), True), ("dashboard:abc", object(), True)])
    def test_the_live_slot_decides(self, key: str, slot: Any, expected: bool) -> None:
        assert _restricted(key, slot) is expected

    @pytest.mark.parametrize("restricted,events", [(True, 1), (False, 0)])
    def test_only_a_denied_upload_is_sel_audited(self, monkeypatch: pytest.MonkeyPatch, restricted: bool, events: int) -> None:
        from kiro_crew.messaging import upload_gate as ug
        seen: list[dict] = []
        monkeypatch.setattr(ug, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: seen.append(kw)))
        assert _restricted("dashboard:abc", _slot(restricted)) is restricted
        assert len(seen) == events
        if events:
            keys = ("outcome", "source", "error", "caller")
            assert tuple(seen[0][key] for key in keys) == ("denied", "discord", "restricted_session", "dashboard:abc")

    def test_no_live_slot_falls_through_to_the_persisted_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.messaging import upload_gate as ug
        monkeypatch.setattr(
            ug, "_persisted_mode_is_restricted", lambda key, probe: key == "dashboard:ghost"
        )
        assert _restricted("dashboard:ghost") is True
        assert _restricted("dashboard:kept") is False

    @pytest.mark.parametrize("mode,restricted", [("incognito", True), ("temporary", True), ("persistent", False), (None, True)])
    def test_the_persisted_mode_decides(self, monkeypatch: pytest.MonkeyPatch, mode: Any, restricted: bool) -> None:
        from kiro_crew.dashboard.handlers import _shared
        from kiro_crew.messaging import upload_gate as ug

        # Injected, not imported: `messaging` may not reach `dashboard`, so the
        # probe travels as an argument and the test supplies it directly.
        probe = _shared._probe_persisted_session
        monkeypatch.setattr(_shared, "_probe_persisted_session", lambda name: (True, mode))
        assert ug._persisted_mode_is_restricted("dashboard:abc", lambda n: (True, mode)) is restricted
        assert probe is not None

    def test_an_ambiguous_stem_denies_instead_of_taking_the_first_match(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from kiro_crew.dashboard.handlers import _shared
        from kiro_crew.messaging import upload_gate as ug
        _stage_sessions(monkeypatch, tmp_path, abc="persistent", dashboard_abc="incognito")
        assert _shared._persisted_session_memory_mode("abc") == "persistent"
        assert _shared._probe_persisted_session("abc") == (True, None)
        assert (
            ug._persisted_mode_is_restricted(
                "dashboard:abc", _shared._probe_persisted_session
            )
            is True
        )

    def test_a_single_unambiguous_persistent_transcript_still_allows(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from kiro_crew.dashboard.handlers import _shared
        from kiro_crew.messaging import upload_gate as ug

        _stage_sessions(monkeypatch, tmp_path, dashboard_solo="persistent")
        assert (
            ug._persisted_mode_is_restricted(
                "dashboard:solo", _shared._probe_persisted_session
            )
            is False
        )

    def test_an_unreadable_probe_denies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.dashboard.handlers import _shared
        from kiro_crew.messaging import upload_gate as ug

        def _boom(name: str) -> Any:
            raise OSError("sessions dir gone")

        assert ug._persisted_mode_is_restricted("dashboard:abc", _boom) is True
        assert _shared is not None  # the real probe is unused here, by design


class TestDescriptionRedaction:
    @pytest.mark.parametrize("streamed", [f"key AKIA\\{_KEY[4:]} here", f"https://x.example.com/u?k=AKIA\\{_KEY[4:]}", f"{_KEY[:4]}\u200b{_KEY[4:]}"])
    def test_a_display_reassembled_credential_is_redacted(self, streamed: str) -> None:
        from kiro_crew.messaging.outbound_files import unescape_md
        from kiro_crew.security import redact_credentials
        assert redact_credentials(streamed)[0] == streamed
        alt = unescape_md(streamed)
        assert _KEY in canonicalize_display(alt)
        assert _KEY not in canonicalize_display(_description(alt))

    def test_redaction_runs_before_the_length_cap(self) -> None:
        from kiro_crew.discord.client import _safe_description
        out = _safe_description("x" * 1010 + _KEY)
        assert len(out) <= 1024
        assert _KEY not in out

    def test_ordinary_alt_survives_intact(self) -> None:
        assert _description("Q1 revenue by region") == "Q1 revenue by region"


class TestRotationNeverBisectsMarkup:
    @pytest.mark.parametrize("stage", ["closed", "dest_open", "label_open", "open_before_closed"])
    @pytest.mark.asyncio
    async def test_a_reference_straddling_the_cut_is_held_for_the_semantic_seal(self, tmp_path: Path, stage: str) -> None:
        r, cli = _renderer()
        limit, png = r._limit(), _png(tmp_path)
        markup = {"closed": f"![Revenue]({png})", "dest_open": f"![Revenue]({png}", "label_open": "![Reve", "open_before_closed": f"![Outer]({png} ![Inner]({png})"}[stage]
        r._buf = ["x" * (limit - len(markup) // 2) + markup + " tail"]
        await r._rotate_on_length()
        assert markup in "".join(r._buf)
        assert all("![" not in text for text, _components in cli.sent)

    @pytest.mark.asyncio
    async def test_the_image_is_delivered_intact_across_a_rotation(self, tmp_path: Path) -> None:
        path = _png(tmp_path)
        r, _ = _renderer()
        markup = f"![Revenue]({path})"
        cli = await _turn("x" * (r._limit() - len(markup) // 2) + markup + " and then some tail text.")
        assert cli.uploads and len(cli.uploaded_files) == 1
        assert cli.uploaded_files[0].data == path.read_bytes()
        for body in _bodies(cli):
            assert "![Revenue]" not in body and str(path) not in body and "](" not in body

    @pytest.mark.asyncio
    async def test_a_reference_wider_than_the_streaming_limit_stays_whole(self, monkeypatch: pytest.MonkeyPatch) -> None:
        markup = "![a](/tmp/" + "d" * 200 + ".png)"
        r, cli = _renderer()
        monkeypatch.setattr(r, "_limit", lambda: 50)
        r._buf = [markup]
        await r._rotate_on_length()
        assert "".join(r._buf) == markup
        assert cli.sent == []

    def test_inline_code_is_literal_across_shared_scans(self, tmp_path: Path) -> None:
        inline = "~~~\n`\n~~~\n``example\n" + ("x" * 80 + "\n") * 25 + f"`![a]({_png(tmp_path)})` and ![b]({_png(tmp_path)})\n``"
        assert not (result := outbound.extract_local_refs(inline)).files and result.rewritten_text == inline and not asyncio.run(_turn(inline)).uploaded_files and not asyncio.run(_turn((inline[: inline.index("`![a]")], inline[inline.index("`![a]"):]))).uploaded_files and outbound.hide_local_refs(inline) == inline and outbound.open_ref_start(inline) is None and outbound.protected_ref_spans(inline) == []


class TestMultipartWire:
    def test_extract_limits_carry_discord_ceilings(self) -> None:
        assert _UPLOAD_LIMITS.max_files == DISCORD_MAX_FILES_PER_MESSAGE and _UPLOAD_LIMITS.max_file_bytes == DISCORD_MAX_FILE_BYTES and _UPLOAD_LIMITS.max_total_bytes == DISCORD_MAX_TOTAL_UPLOAD_BYTES and DISCORD_MAX_TOTAL_UPLOAD_BYTES < DISCORD_MAX_FILES_PER_MESSAGE * DISCORD_MAX_FILE_BYTES

    def test_payload_json_leads_and_descriptors_name_their_parts(self) -> None:
        fields = _fields({"content": "hi"}, [_file("/tmp/a.png", alt="First"), _file("/tmp/b.jpg", data=_JPEG, mime="image/jpeg")])
        assert [o["name"] for o, _h, _v in fields] == ["payload_json", "files[0]", "files[1]"]
        assert (payload := json.loads(fields[0][2]))["content"] == "hi"
        assert [a["id"] for a in payload["attachments"]] == [0, 1]
        assert [a["filename"] for a in payload["attachments"]] == [fields[1][0]["filename"], fields[2][0]["filename"]]
        assert payload["attachments"][0]["description"] == "First" and "description" not in payload["attachments"][1]
        assert fields[2][1]["Content-Type"] == "image/jpeg"

    def test_the_form_carries_bytes_and_never_reopens_the_path(self) -> None:
        assert _fields({"content": ""}, [_file("/nonexistent/dir/chart.png")])[1][2] == _PNG

    @pytest.mark.parametrize("raw,mime,expected", [("/tmp/chart.png", "image/jpeg", "chart.jpg"), ("/tmp/deep/dir/shot.png", "image/png", "shot.png"), ('/tmp/no"quotes.png', "image/png", None), ("/tmp/no\nnewline.png", "image/png", None), ("/tmp/../../etc/passwd.png", "image/png", None), ("/tmp/.hidden.png", "image/png", None), ("/tmp/ghp:" + "a" * 36 + ".png", "image/png", "image_0.png")])
    def test_the_upload_filename_is_derived_not_trusted(self, raw: str, mime: str, expected: str | None) -> None:
        name = upload_filename(_file(raw, mime=mime), 0)
        if expected is not None:
            assert name == expected
        assert all(c.isalnum() or c in "._-" for c in name) and not name.startswith(".") and "/" not in name and ".." not in name, name

    def test_an_unusable_name_falls_back_without_colliding(self) -> None:
        names = [upload_filename(_file("/tmp/" + c * 4), i) for i, c in enumerate("!?")]
        assert len(set(names)) == 2 and all(n.endswith(".png") for n in names), names

    def test_a_429_retry_rebuilds_the_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from contextlib import asynccontextmanager

        from kiro_crew.discord.client import DiscordClient
        bodies: list[Any] = []
        statuses = iter((429, 200))

        @asynccontextmanager
        async def request(method: str, url: str, **kw: Any) -> Any:
            bodies.append(kw.get("data"))
            status = next(statuses)

            async def response_json(content_type: Any = None) -> Any:
                return {"retry_after": 0.0} if status == 429 else {"id": "42"}
            yield SimpleNamespace(status=status, json=response_json)

        session = SimpleNamespace(request=request)
        client = DiscordClient(token="t")
        monkeypatch.setattr(client, "_ensure_session", lambda: _done(session))
        monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
        assert asyncio.run(client.send_message_with_files("c1", "hi", [_file()])) == "42" and len(bodies) == 2 and bodies[0] is not bodies[1]
        for body in bodies:
            assert any(_PNG == value for _o, _h, value in body._fields)

    def test_bodyless_2xx_is_still_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.discord.client import DiscordClient
        client = DiscordClient(token="t")
        monkeypatch.setattr(client, "_api_multipart", lambda *a, **kw: _done({}))
        assert asyncio.run(client.send_message_with_files("c1", "hi", [_file()])) == ""


class TestDocumentVerb:
    """The name-preserving document send (issue #6058).

    Separate from ``send_message_with_files`` on purpose: that path's sanitizer
    is aimed at LLM-authored reference paths and maps every non-raster mime to
    ``.bin``, so it would deliver ``report.pdf`` as ``report.bin``. These tests
    pin BOTH halves — the admitted name survives here, and the extraction path's
    derivation is untouched.
    """

    def test_the_extraction_path_still_derives_the_name(self) -> None:
        # The guarantee `upload_filename` makes to its existing callers: an
        # untrusted path, a non-raster mime, `.bin`. Unchanged by this verb.
        assert upload_filename(_doc(), 0) == "report.bin"
        fields = _fields({"content": ""}, [_doc()])
        assert json.loads(fields[0][2])["attachments"][0]["filename"] == "report.bin"
        assert fields[1][0]["filename"] == "report.bin"

    def test_a_pinned_name_survives_into_descriptor_and_part(self) -> None:
        # Discord matches a descriptor to its part by `id`; a name that differs
        # between the two is what renames an attachment mid-upload.
        fields = _fields({"content": ""}, [_doc()], filenames=["report.pdf"])
        assert json.loads(fields[0][2])["attachments"][0]["filename"] == "report.pdf"
        assert fields[1][0]["filename"] == "report.pdf" and fields[1][2] == b"%PDF-1.4 body"

    def test_a_short_pin_list_falls_back_per_file(self) -> None:
        fields = _fields({"content": ""}, [_doc(), _file("/tmp/b.png")], filenames=["report.pdf"])
        assert [a["filename"] for a in json.loads(fields[0][2])["attachments"]] == ["report.pdf", "b.png"]

    def test_send_document_pins_the_basename_on_the_wire(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, seen = _client(monkeypatch, {"id": "77"})
        assert asyncio.run(client.send_document("c1", _doc(), caption="weekly numbers")) == "77"
        form = seen[-1]["data"]._fields
        payload = json.loads(form[0][2])
        assert payload["content"] == "weekly numbers"
        assert payload["attachments"] == [{"id": 0, "filename": "report.pdf"}]
        assert form[1][0]["filename"] == "report.pdf" and form[1][2] == b"%PDF-1.4 body"

    def test_a_document_over_the_upload_ceiling_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # One attachment IS the whole upload, so the message ceiling is this
        # file's ceiling. Refused locally rather than uploaded into a 413.
        client, seen = _client(monkeypatch, {"id": "77"})
        over = _doc(data=b"x" * (DISCORD_MAX_TOTAL_UPLOAD_BYTES + 1))
        assert asyncio.run(client.send_document("c1", over)) is None and seen == []
        assert asyncio.run(client.send_document("c1", _doc(data=b""))) is None and seen == []

    def test_the_caption_is_clamped_to_the_content_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, seen = _client(monkeypatch, {"id": "77"})
        assert asyncio.run(client.send_document("c1", _doc(), caption="c" * (DISCORD_MAX_TEXT + 50))) == "77"
        assert len(json.loads(seen[-1]["data"]._fields[0][2])["content"]) == DISCORD_MAX_TEXT

    def test_the_transport_returns_the_message_id_and_routes_a_thread(self) -> None:
        transport = DiscordTransport(cli := FakeClient(), allowed_user_ids=["u1"])  # type: ignore[arg-type]
        doc = _doc()
        assert asyncio.run(transport.send_document("c1", doc, caption="hi")).isdigit()
        # A Discord thread's snowflake IS its channel id, so a supplied
        # thread_id is the destination rather than a second parameter.
        assert asyncio.run(transport.send_document("c1", doc, thread_id="t9")).isdigit()
        assert [(chan, f is doc, cap) for chan, f, cap in cli.documents] == [("c1", True, "hi"), ("t9", True, None)]

    def test_a_refused_transport_send_reports_an_empty_id(self) -> None:
        transport = DiscordTransport(cli := FakeClient(), allowed_user_ids=["u1"])  # type: ignore[arg-type]
        cli.fail_uploads = True
        assert asyncio.run(transport.send_document("c1", _doc())) == ""


async def _done(value: Any) -> Any:
    return value


async def _noop_sleep(_seconds: float) -> None:
    return None


class TestDisplayRedactionIsAFloor:
    """Display-form redaction runs on every send, not only the upload path.

    ``TurnDriver`` redacts the LITERAL form of every chunk upstream. The display
    pass exists for the credential that is invisible until Discord renders the
    markdown away, so gating it on the upload path left a restricted session, an
    unset upload root, and every length rotation sending model text that only the
    literal-form redactor had seen.
    """

    #: A credential split by Markdown that Discord strips when it renders. The
    #: literal bytes carry ``**``, so a literal-form scan does not match; the
    #: rendered form is one contiguous key.
    SPLIT_SECRET = "AKIA**IOSFODNN7**EXAMPLE"

    @pytest.mark.asyncio
    async def test_a_restricted_session_still_gets_the_display_pass(self) -> None:
        cli = await _turn(f"here it is {self.SPLIT_SECRET}", uploads_allowed=False)
        body = cli.final_text()
        assert "IOSFODNN7" not in body, body

    @pytest.mark.asyncio
    async def test_no_upload_root_still_gets_the_display_pass(self) -> None:
        cli = await _turn(f"here it is {self.SPLIT_SECRET}", upload_root="")
        assert "IOSFODNN7" not in cli.final_text()

    @pytest.mark.asyncio
    async def test_a_channel_without_files_outbound_still_gets_the_display_pass(self) -> None:
        caps = replace(DISCORD_CAPABILITIES, files_outbound=False)
        cli = await _turn(f"here it is {self.SPLIT_SECRET}", capabilities=caps)
        assert "IOSFODNN7" not in cli.final_text()

    @pytest.mark.asyncio
    async def test_a_length_rotated_segment_still_gets_the_display_pass(self) -> None:
        """Length seals pass ``extract_uploads=False``, which was the widest of
        the ungated routes: it is reached on any reply long enough to rotate."""
        r, cli = _renderer()
        await r.on_text_chunk("y" * (r._limit() + 50) + f"\n\n{self.SPLIT_SECRET}\n")
        await r.on_done()
        assert all("IOSFODNN7" not in text for text in _bodies(cli))

    @pytest.mark.asyncio
    async def test_mentions_are_defanged_on_an_ungated_send_too(self) -> None:
        cli = await _turn("ping @everyone now", uploads_allowed=False)
        # The text survives; only the ping is broken (the zero-width space is the
        # renderer's half, `allowed_mentions` is the transport's).
        assert "@​everyone" in cli.final_text()


class TestDeliveryAccountingCountsEverySink:
    """`delivery_failed` answers "the user saw NOTHING", so every sink must report.

    The dispatcher records a turn's outcome from what the provider produced, which
    says nothing about whether anything arrived; `delivery_failed` is the
    observable that separates them, and a cron turn acts on it (it re-alerts over
    Slack and refuses to advance its dedup hash). So a sink that delivers without
    reporting turns a success into a duplicate alert, and a sink that fails
    without reporting turns a silent turn into a recorded success.
    """

    @pytest.mark.asyncio
    async def test_a_markup_recovery_that_lands_is_not_a_failed_delivery(self, tmp_path: Path) -> None:
        """The upload failed and the seal with it, but the recovery post arrived."""
        path, (r, cli) = _png(tmp_path), _renderer()
        cli.edit_ok, cli.fail_uploads = False, True
        await r.on_text_chunk(f"Here it is.\n\n![Revenue]({path})")
        await r.on_done()
        assert cli.uploads, "premise: the upload was attempted"
        assert [t for t, _ in cli.sent], "premise: the recovery post went out"
        assert r.delivery_failed is False

    @pytest.mark.asyncio
    async def test_a_recovery_that_also_fails_is_a_failed_delivery(self, tmp_path: Path) -> None:
        path, (r, cli) = _png(tmp_path), _renderer()
        cli.edit_ok, cli.fail_uploads, cli.fail_sends = False, True, True
        await r.on_text_chunk(f"Here it is.\n\n![Revenue]({path})")
        await r.on_done()
        assert r.delivery_failed is True

    @pytest.mark.asyncio
    async def test_a_placeholder_that_fails_is_a_failed_delivery(self) -> None:
        """An empty-bodied turn's placeholder IS the whole delivery."""
        r, cli = _renderer()
        cli.fail_sends = True
        await r.on_text_chunk("   ")
        await r.on_done()
        assert [t for t, _ in cli.sent], "premise: a placeholder was attempted"
        assert r.delivery_failed is True

    @pytest.mark.asyncio
    async def test_a_placeholder_that_lands_is_not_a_failed_delivery(self) -> None:
        r, cli = _renderer()
        await r.on_text_chunk("   ")
        await r.on_done()
        assert r.delivery_failed is False
