"""Guards for vendor-API fixture provenance and shape conformance.

A wire fixture asserts something about someone else's API. These tests make
that assertion auditable: every fixture must say where its shape came from, an
unverified fixture must be visible as such rather than silently authoritative,
and a live response can be diffed against a stored fixture by shape alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.testing.channel_fixtures import (
    Provenance,
    ShapeMismatch,
    Source,
    assert_same_shape,
    iter_fixtures,
    load_fixture,
    shape_of,
    unverified,
    write_fixture,
)

# The fixtures root lives in the TEST tree, so the layout coupling lives here
# too -- kiro_crew.testing.channel_fixtures ships in the wheel and deliberately
# has no default root (no test/ tree exists in an installed package).
CHANNEL_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "channels"


class TestFixtureIdentifiersCannotEscapeTheRoot:
    """``channel``/``name`` are interpolated into a path, so they are validated.

    Without this, ``load_fixture("../../etc", "passwd")`` would read outside the
    root and ``write_fixture`` would OVERWRITE outside it -- arbitrary local file
    access under the user's permissions.
    """

    @pytest.mark.parametrize(
        "bad",
        [
            "..",
            ".",
            "../outside",
            "a/b",
            "a\\b",
            "/abs",
            "",
            "nul\x00byte",
        ],
    )
    def test_traversal_and_separators_are_rejected_for_channel(self, bad, tmp_path) -> None:
        with pytest.raises(ValueError):
            load_fixture(bad, "x", root=tmp_path)

    @pytest.mark.parametrize("bad", ["C:", "a:", "Z:"])
    def test_a_bare_drive_component_is_rejected(self, bad, tmp_path) -> None:
        """``root / "C:"`` discards the root entirely on Windows.

        A bare drive survives the separator, absolute and parts checks --
        ``Path("C:").parts == ("C:",)`` and ``is_absolute()`` is False -- but
        joining it drops the root and yields a drive-relative path. Rejected via
        PureWindowsPath so the guard holds when authoring on POSIX too.
        """
        with pytest.raises(ValueError, match="drive"):
            load_fixture(bad, "x", root=tmp_path)
        with pytest.raises(ValueError, match="drive"):
            load_fixture("weixin", bad, root=tmp_path)

    @pytest.mark.parametrize("bad", [".. ", "...", "..  ", ". ", "foo.", "foo "])
    def test_windows_stripped_padding_is_rejected(self, bad, tmp_path) -> None:
        """Windows strips trailing dots and spaces before the filesystem sees them.

        So ".. " IS ".." there, and "foo." IS "foo" -- padding must never be a
        way to smuggle a dot segment past the traversal check.
        """
        with pytest.raises(ValueError):
            load_fixture(bad, "x", root=tmp_path)

    def test_a_drive_relative_path_is_still_rejected(self, tmp_path) -> None:
        # "C:foo" is two parts, so the existing component check already caught
        # it -- pinned so a refactor of the drive check cannot regress it.
        with pytest.raises(ValueError):
            load_fixture("C:foo", "x", root=tmp_path)

    @pytest.mark.parametrize("bad", ["..", "a/b", "a\\b", "/abs", ""])
    def test_traversal_and_separators_are_rejected_for_name(self, bad, tmp_path) -> None:
        with pytest.raises(ValueError):
            load_fixture("weixin", bad, root=tmp_path)

    def test_write_is_validated_before_touching_the_filesystem(self, tmp_path) -> None:
        outside = tmp_path.parent / "escaped.json"
        with pytest.raises(ValueError):
            write_fixture(
                "../..",
                "escaped",
                {"a": 1},
                Provenance(Source.ASSUMED, reference="attack"),
                root=tmp_path,
            )
        assert not outside.exists(), "write_fixture must reject before mkdir/write"

    def test_a_valid_identifier_still_resolves_inside_the_root(self, tmp_path) -> None:
        path = write_fixture(
            "weixin",
            "ok",
            {"a": 1},
            Provenance(Source.ASSUMED, reference="unit"),
            root=tmp_path,
        )
        assert path.parent.parent == tmp_path


class TestNoImplicitFixturesRoot:
    """The shipped module must not guess a root.

    ``kiro_crew.testing`` is in the runtime wheel, where no ``test/`` tree
    exists, so any ``__file__``-derived default would resolve to an unpackaged
    path and fail for every installed consumer. The caller owns the layout.
    """

    def test_root_is_a_required_keyword(self) -> None:
        import inspect

        for fn in (load_fixture, iter_fixtures, unverified, write_fixture):
            sig = inspect.signature(fn)
            assert sig.parameters["root"].default is inspect.Parameter.empty, (
                f"{fn.__name__} must require an explicit root -- a default derived "
                f"from __file__ is wrong in an installed wheel"
            )

    def test_a_missing_root_directory_yields_no_fixtures_rather_than_raising(
        self, tmp_path
    ) -> None:
        assert iter_fixtures(root=tmp_path / "nope") == []


class TestProvenanceIsMandatory:
    def test_every_committed_fixture_declares_a_known_source(self) -> None:
        fixtures = iter_fixtures(root=CHANNEL_FIXTURES)
        assert fixtures, "no channel wire fixtures found -- did the tree move?"
        for fx in fixtures:
            assert isinstance(fx.provenance.source, Source)
            assert fx.provenance.reference, (
                f"{fx.channel}/{fx.name} names a source but no reference; a claim "
                f"about a vendor API must say where it came from"
            )

    def test_verified_fixtures_record_when_they_were_captured(self) -> None:
        for fx in iter_fixtures(root=CHANNEL_FIXTURES):
            if fx.provenance.source is Source.LIVE_PROBE:
                assert fx.provenance.captured_at, (
                    f"{fx.channel}/{fx.name} claims a live probe but no date -- "
                    f"staleness must be visible"
                )

    def test_a_fixture_without_provenance_is_rejected(self, tmp_path) -> None:
        d = tmp_path / "weixin"
        d.mkdir(parents=True)
        (d / "bare.json").write_text(json.dumps({"errcode": 0}), encoding="utf-8")

        with pytest.raises(ValueError, match="_provenance"):
            load_fixture("weixin", "bare", root=tmp_path)

    def test_unknown_source_is_rejected(self, tmp_path) -> None:
        d = tmp_path / "weixin"
        d.mkdir(parents=True)
        (d / "odd.json").write_text(
            json.dumps({"_provenance": {"source": "vibes"}, "payload": {}}),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="known source"):
            load_fixture("weixin", "odd", root=tmp_path)


class TestUnverifiedInventory:
    def test_unverified_fixtures_are_enumerable(self) -> None:
        # Not an assertion that the list is empty -- it is not, and pretending
        # otherwise is the failure mode this module exists to prevent. The
        # contract is that the gap is KNOWN.
        names = {f"{f.channel}/{f.name}" for f in unverified(root=CHANNEL_FIXTURES)}
        assert "weixin/sendmessage_ok" in names, (
            "the sendmessage success body has never been captured from iLink; "
            "it must keep reporting as unverified until it is"
        )

    def test_the_live_probed_qr_shape_is_verified(self) -> None:
        fx = load_fixture("weixin", "get_bot_qrcode", root=CHANNEL_FIXTURES)
        assert fx.is_verified
        assert fx.provenance.source is Source.LIVE_PROBE
        # The shape claim that PR #711 established.
        assert fx.payload["qrcode_img_content"].startswith("https://")


class TestNoSilentDowngrade:
    def test_verified_fixture_cannot_be_overwritten_as_assumed(self, tmp_path) -> None:
        write_fixture(
            "weixin",
            "probe",
            {"a": 1},
            Provenance(Source.LIVE_PROBE, reference="probe", captured_at="2026-07-28"),
            root=tmp_path,
        )

        with pytest.raises(ValueError, match="refusing to downgrade"):
            write_fixture(
                "weixin",
                "probe",
                {"a": 1},
                Provenance(Source.ASSUMED, reference="guess"),
                root=tmp_path,
            )

    def test_downgrade_is_possible_when_explicit(self, tmp_path) -> None:
        write_fixture(
            "weixin",
            "probe",
            {"a": 1},
            Provenance(Source.LIVE_PROBE, reference="probe", captured_at="2026-07-28"),
            root=tmp_path,
        )
        write_fixture(
            "weixin",
            "probe",
            {"a": 1},
            Provenance(Source.ASSUMED, reference="deliberate"),
            root=tmp_path,
            allow_downgrade=True,
        )
        assert not load_fixture("weixin", "probe", root=tmp_path).is_verified


class TestShapeComparison:
    def test_values_are_ignored_so_volatile_data_never_flakes(self) -> None:
        fixture = {"qrcode": "7GiQu1", "expires_in": 300}
        live = {"qrcode": "ZZ9pluralZalpha", "expires_in": 600}
        assert_same_shape(fixture, live)  # different values, same shape

    def test_a_missing_field_is_reported_by_name(self) -> None:
        with pytest.raises(ShapeMismatch, match="qrcode_img_content"):
            assert_same_shape(
                {"qrcode": "a", "qrcode_img_content": "https://x"}, {"qrcode": "a"}
            )

    def test_a_retyped_field_is_reported_with_both_types(self) -> None:
        # The exact drift class behind #711: a field that was a string
        # coming back as something else (or vice versa).
        with pytest.raises(ShapeMismatch, match="fixture says str, response has int"):
            assert_same_shape({"errcode": "0"}, {"errcode": 0})

    def test_an_added_field_marks_the_fixture_stale(self) -> None:
        with pytest.raises(ShapeMismatch, match="fixture is stale"):
            assert_same_shape({"a": 1}, {"a": 1, "b": 2})

    def test_nested_paths_are_named_in_full(self) -> None:
        with pytest.raises(ShapeMismatch, match=r"msg\.item_list"):
            assert_same_shape({"msg": {"item_list": [{"type": 1}]}}, {"msg": {}})

    def test_list_length_does_not_matter_only_element_shape(self) -> None:
        one = {"msgs": [{"text": "a"}]}
        many = {"msgs": [{"text": "a"}, {"text": "b"}, {"text": "c"}]}
        assert_same_shape(one, many)

    def test_heterogeneous_list_elements_are_distinguished(self) -> None:
        assert shape_of([{"a": 1}]) != shape_of([{"a": 1}, {"b": 2}])

    def test_empty_containers_compare_equal_to_themselves(self) -> None:
        assert_same_shape({"msgs": []}, {"msgs": []})


class TestRouteScriptSemantics:
    """A sequence route is an exact script, not a repeating steady state."""

    def test_a_shared_sequence_is_copied_not_consumed(self) -> None:
        from kiro_crew.testing.fake_channel_wire import FakeWireSession

        shared = [{"n": 1}, {"n": 2}]
        wire = FakeWireSession().route("POST", "/x", shared)
        wire._match("POST", "/x")
        wire._match("POST", "/x")

        assert shared == [{"n": 1}, {"n": 2}], "the caller's script must be untouched"

    def test_calling_past_the_script_raises_instead_of_repeating(self) -> None:
        from kiro_crew.testing.fake_channel_wire import (
            FakeWireSession,
            UnroutedRequestError,
        )

        wire = FakeWireSession().route("POST", "/x", [{"n": 1}, {"n": 2}])
        wire._match("POST", "/x")
        wire._match("POST", "/x")

        # Repeating the last response would hide a client that polled more times
        # than the test scripted -- the same bug class fail-closed routing exists
        # to catch.
        with pytest.raises(UnroutedRequestError, match="exhausted"):
            wire._match("POST", "/x")

    def test_a_tuple_target_is_a_script_not_a_response_body(self) -> None:
        from kiro_crew.testing.fake_channel_wire import FakeWireSession

        # RouteTarget declares Iterable; handling only list would have used the
        # tuple itself as the response body.
        wire = FakeWireSession().route("POST", "/x", ({"n": 1}, {"n": 2}))

        assert wire._match("POST", "/x") == {"n": 1}
        assert wire._match("POST", "/x") == {"n": 2}

    def test_a_single_target_answers_every_call(self) -> None:
        from kiro_crew.testing.fake_channel_wire import FakeWireSession

        wire = FakeWireSession().route("POST", "/x", {"n": 1})

        assert [wire._match("POST", "/x") for _ in range(3)] == [{"n": 1}] * 3


class TestSymlinkContainment:
    """Component validation is not containment.

    A pre-existing symlink at <root>/<channel> points wherever it likes while
    every component looks innocent, so the canonical path is checked too.
    """

    def test_a_dangling_symlink_at_the_leaf_is_refused(self, tmp_path) -> None:
        """exists() is False for a BROKEN link, but open() still follows it.

        So a walk gated on exists() would skip the link as "nonexistent", pass
        containment, and then write straight through it to the link's target.
        """
        root = tmp_path / "fixtures"
        (root / "weixin").mkdir(parents=True)
        outside = tmp_path / "outside" / "stolen.json"
        outside.parent.mkdir()
        (root / "weixin" / "x.json").symlink_to(outside)  # dangling: target absent

        with pytest.raises(ValueError, match="escapes the root"):
            write_fixture(
                "weixin", "x", {"a": 1}, Provenance(Source.ASSUMED, reference="t"), root=root
            )
        assert not outside.exists(), "must not write through a dangling symlink"

    def test_a_dangling_symlink_is_refused_on_read_too(self, tmp_path) -> None:
        root = tmp_path / "fixtures"
        (root / "weixin").mkdir(parents=True)
        (root / "weixin" / "x.json").symlink_to(tmp_path / "outside" / "nope.json")

        with pytest.raises(ValueError, match="escapes the root"):
            load_fixture("weixin", "x", root=root)

    def test_a_root_that_does_not_exist_yet_is_created_not_refused(self, tmp_path) -> None:
        """A missing root is normal -- write_fixture creates it.

        The containment walk must stop AT the root rather than climbing above
        it, or a legitimate first write into a fresh directory is rejected.
        """
        fresh = tmp_path / "brand" / "new" / "root"

        path = write_fixture(
            "weixin", "ok", {"a": 1}, Provenance(Source.ASSUMED, reference="t"), root=fresh
        )

        assert path.is_file()
        assert load_fixture("weixin", "ok", root=fresh).payload == {"a": 1}

    def test_a_symlinked_channel_directory_is_refused_on_read(self, tmp_path) -> None:
        root = tmp_path / "fixtures"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "x.json").write_text('{"_provenance": {"source": "assumed"}}', encoding="utf-8")
        (root / "weixin").symlink_to(outside, target_is_directory=True)

        with pytest.raises(ValueError, match="escapes the root"):
            load_fixture("weixin", "x", root=root)

    def test_a_symlinked_channel_directory_is_refused_before_write(self, tmp_path) -> None:
        root = tmp_path / "fixtures"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "weixin").symlink_to(outside, target_is_directory=True)

        with pytest.raises(ValueError, match="escapes the root"):
            write_fixture(
                "weixin", "x", {"a": 1}, Provenance(Source.ASSUMED, reference="t"), root=root
            )
        assert not (outside / "x.json").exists(), "must refuse BEFORE writing"

    def test_a_real_directory_under_the_root_still_works(self, tmp_path) -> None:
        path = write_fixture(
            "weixin", "ok", {"a": 1}, Provenance(Source.ASSUMED, reference="t"), root=tmp_path
        )
        assert path.is_file()
        assert load_fixture("weixin", "ok", root=tmp_path).payload == {"a": 1}

    def test_a_mid_write_failure_does_not_double_close_the_descriptor(
        self, tmp_path, monkeypatch
    ) -> None:
        """The fd must be closed exactly once, by exactly one owner.

        If the write raises inside the ``with``, the file object closes the fd --
        and a second close of the same NUMBER could shut an unrelated file or
        socket that another thread opened in between. This drives a failure
        through the write path and counts the closes on that descriptor.
        """
        import os as _os

        from kiro_crew.testing import channel_fixtures as cf

        closed: list[int] = []
        real_close = _os.close
        monkeypatch.setattr(_os, "close", lambda fd: (closed.append(fd), real_close(fd))[1])

        class Boom(RuntimeError):
            pass

        real_fdopen = _os.fdopen

        def _exploding_fdopen(fd, *a, **kw):
            handle = real_fdopen(fd, *a, **kw)
            original_write = handle.write

            def _write(_text):  # raise AFTER the file object owns the fd
                original_write("")
                raise Boom("write failed")

            monkeypatch.setattr(handle, "write", _write, raising=False)
            return handle

        monkeypatch.setattr(_os, "fdopen", _exploding_fdopen)

        with pytest.raises(Boom):
            cf._write_no_follow(tmp_path / "x.json", "payload")

        assert len(closed) == len(set(closed)), (
            f"a descriptor was closed twice: {closed} -- another thread could have "
            f"reused that number between the closes"
        )

    def test_a_failed_write_preserves_the_existing_fixture(self, tmp_path, monkeypatch) -> None:
        """A write that dies mid-flight must not destroy the payload it replaces.

        Opening the target with O_TRUNC truncates at OPEN time, so a full disk
        would leave an empty file and lose bytes that may have cost a
        credentialled live probe to capture. The old content must survive whole.
        """
        import os as _os

        from kiro_crew.testing import channel_fixtures as cf

        target = tmp_path / "x.json"
        target.write_text('{"original": true}', encoding="utf-8")

        real_fdopen = _os.fdopen

        def _exploding_fdopen(fd, *a, **kw):
            handle = real_fdopen(fd, *a, **kw)

            def _write(_text):  # simulate ENOSPC part-way through
                raise OSError(28, "No space left on device")

            monkeypatch.setattr(handle, "write", _write, raising=False)
            return handle

        monkeypatch.setattr(_os, "fdopen", _exploding_fdopen)

        with pytest.raises(OSError):
            cf._write_no_follow(target, '{"replacement": true}')

        assert target.read_text(encoding="utf-8") == '{"original": true}', (
            "a failed write truncated the existing fixture"
        )
        strays = [p.name for p in tmp_path.iterdir() if p.name != "x.json"]
        assert strays == [], f"a partial temporary was left behind: {strays}"

    def test_a_successful_write_leaves_no_temporary_behind(self, tmp_path) -> None:
        """The happy path must replace the target and clean up after itself."""
        from kiro_crew.testing import channel_fixtures as cf

        target = tmp_path / "x.json"
        cf._write_no_follow(target, '{"new": true}')

        assert target.read_text(encoding="utf-8") == '{"new": true}'
        assert [p.name for p in tmp_path.iterdir()] == ["x.json"]

    def test_a_symlinked_leaf_is_reported_not_silently_replaced(self, tmp_path) -> None:
        """os.replace would swap the LINK for a file -- no escape, but silent.

        The write never follows the link either way; the explicit check is what
        turns a confusing silent success into a stated refusal.
        """
        from kiro_crew.testing import channel_fixtures as cf

        outside = tmp_path / "outside.json"
        outside.write_text("untouched", encoding="utf-8")
        link = tmp_path / "x.json"
        link.symlink_to(outside)

        with pytest.raises(ValueError, match="symlink"):
            cf._write_no_follow(link, '{"new": true}')

        assert outside.read_text(encoding="utf-8") == "untouched"
        assert link.is_symlink(), "the symlink itself must be left alone"

    def test_a_non_dict_mapping_is_a_response_body_not_a_script(self) -> None:
        """Mapping, not dict: a MappingProxyType is Iterable over its KEYS.

        A dict-only exclusion would turn an ordinary response body into a
        script of its key strings.
        """
        from types import MappingProxyType

        from kiro_crew.testing.fake_channel_wire import FakeWireSession

        body = MappingProxyType({"errcode": 0, "msg": "ok"})
        wire = FakeWireSession().route("POST", "/x", body)

        assert wire._match("POST", "/x") == body
        assert wire._match("POST", "/x") == body, "a single target answers every call"

    def test_a_mapping_body_survives_being_read_not_just_routed(self) -> None:
        """Accepting a Mapping as a body is only half the contract.

        Routing it is useless if reading it explodes: json.dumps rejects a
        mappingproxy outright (it is not a dict subclass), so a supported body
        became a TypeError inside the client under test. Nested mappings matter
        too -- a top-level-only conversion would still crash one level down.
        """
        import json as _json
        from types import MappingProxyType

        from kiro_crew.testing.fake_channel_wire import WireResponse

        flat = WireResponse(body=MappingProxyType({"errcode": 0, "msg": "ok"}))
        assert _json.loads(flat.text()) == {"errcode": 0, "msg": "ok"}
        assert _json.loads(flat.raw().decode("utf-8")) == {"errcode": 0, "msg": "ok"}

        nested = WireResponse(
            body={"outer": MappingProxyType({"inner": MappingProxyType({"n": 1})})}
        )
        assert _json.loads(nested.text()) == {"outer": {"inner": {"n": 1}}}

    def test_an_unserializable_body_still_reports_its_type(self) -> None:
        """The fallback must not silently swallow a genuinely bad body."""
        from kiro_crew.testing.fake_channel_wire import WireResponse

        with pytest.raises(TypeError, match="object.*not JSON-serializable|not JSON"):
            WireResponse(body=object()).text()
