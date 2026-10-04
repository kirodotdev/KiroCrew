"""The registry, the instance store, share and snapshots (CONTRACT-v3 parts 4 and 7).

One FIXTURE template throughout, written by these tests. Worker B's built-in templates
land in parallel, and a test reading those would be measuring their work: this suite is
about the machinery, which has to be right for a template nobody has written yet.

Every test runs under its own data home, so a user directory, an instance and a
snapshot are all per test.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.dashboard_templates import catalog, instance
from kiro_crew.dashboard_templates import manifest as manifest_mod
from kiro_crew.dashboard_templates import share, snapshot

PAGE = '<div><b data-dashboard-field="credits"></b>' '<i data-dashboard-field="phase"></i></div>'
#: A second page binding the SAME fields, so an edit that changes only the layout is
#: distinguishable from one that changes the contract.
PAGE_EDITED = (
    '<section><span data-dashboard-field="credits"></span>'
    '<span data-dashboard-field="phase"></span></section>'
)


def _manifest(**over):
    raw = {
        "id": "fixture-board",
        "version": 1,
        "title": "Fixture board",
        "description": "A template these tests own.",
        "source": "builtin",
        "fields": {
            "credits": {"type": "number", "source": {"fold": "usage", "path": "credits"}},
            "phase": {"type": "string", "source": {"agentic": True}},
        },
    }
    raw.update(over)
    return raw


def _write_template(directory, manifest, html=PAGE):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "template.html").write_text(html, encoding="utf-8")
    return directory


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """An isolated data home, and a built-in directory this test owns.

    The built-in root is monkeypatched rather than written into the repo: a test that
    created ``src/.../builtin/fixture-board/`` would leave a template in the product's
    own registry, and every other test and every pod would then load it.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    monkeypatch.setattr(catalog, "builtin_dir", lambda: builtin)
    yield builtin


@pytest.fixture()
def one_builtin(_home):
    return _write_template(_home / "fixture-board", _manifest())


# --------------------------------------------------------------------------
# part 7: the registry
# --------------------------------------------------------------------------


def test_the_registry_loads_a_builtin_and_names_what_it_needs(one_builtin):
    found = catalog.list_templates()
    assert found.problems == ()
    (entry,) = found.entries
    assert entry.id == "fixture-board"
    assert entry.origin == catalog.BUILTIN_SOURCE
    assert entry.html == PAGE
    row = entry.listing()
    assert row["folds"] == ["usage"]
    assert row["agentic"] == ["phase"]
    # The row a chooser draws carries no page: a list of every template would
    # otherwise be a list of every page.
    assert "html" not in row


def test_a_user_template_loads_from_the_data_home(one_builtin):
    _write_template(catalog.user_dir() / "mine", _manifest(id="mine", source="user", title="Mine"))
    by_id = catalog.list_templates().by_id
    assert set(by_id) == {"fixture-board", "mine"}
    assert by_id["mine"].origin == catalog.USER_SOURCE


def test_one_broken_template_is_reported_and_does_not_hide_the_good_ones(one_builtin):
    broken = catalog.user_dir() / "broken"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text("{not json", encoding="utf-8")
    (broken / "template.html").write_text(PAGE, encoding="utf-8")
    found = catalog.list_templates()
    assert [e.id for e in found.entries] == ["fixture-board"]
    assert [name for name, _why in found.problems] == ["broken"]


def test_a_field_whose_type_is_not_a_string_is_isolated_like_any_other_break(one_builtin):
    """A value the manifest's name checks cannot hash is one directory's problem.

    The scanner reports what it cannot load, so the registry owes the good templates
    to every caller however the bad one is spelled.
    """
    _write_template(
        catalog.user_dir() / "unhashable",
        _manifest(
            id="unhashable",
            source="user",
            fields={"credits": {"type": [], "source": {"fold": "usage", "path": "credits"}}},
        ),
        html='<div><b data-dashboard-field="credits"></b></div>',
    )
    found = catalog.list_templates()
    assert [e.id for e in found.entries] == ["fixture-board"]
    ((name, why),) = found.problems
    assert name == "unhashable" and "type []" in why
    # The good template is still serveable by id, not merely listed.
    assert catalog.load_one("fixture-board").html == PAGE


def test_a_manifest_naming_another_id_than_its_directory_is_refused(_home):
    _write_template(_home / "elsewhere", _manifest(id="fixture-board"))
    found = catalog.list_templates()
    assert found.entries == ()
    ((name, why),) = found.problems
    assert name == "elsewhere" and "does not match its directory name" in why


def test_a_user_template_may_not_claim_to_be_a_builtin(_home):
    _write_template(catalog.user_dir() / "faker", _manifest(id="faker", source="builtin"))
    found = catalog.list_templates()
    assert found.entries == ()
    ((name, why),) = found.problems
    assert name == "faker" and "not one of ['shared', 'user']" in why


def test_a_user_template_may_declare_shared(_home):
    _write_template(catalog.user_dir() / "given", _manifest(id="given", source="shared"))
    assert catalog.list_templates().by_id["given"].manifest.source == "shared"


def test_a_user_template_cannot_shadow_a_builtin(one_builtin):
    _write_template(catalog.user_dir() / "fixture-board", _manifest(source="user", version=9))
    found = catalog.list_templates()
    served = found.by_id["fixture-board"]
    assert served.origin == catalog.BUILTIN_SOURCE and served.version == 1
    assert ("fixture-board", "a built-in template already uses this id") in found.problems


def test_an_unknown_template_says_what_is_known(one_builtin):
    with pytest.raises(catalog.UnknownTemplate, match="fixture-board"):
        catalog.load_one("nope")


def test_an_empty_registry_is_not_an_error(_home):
    assert catalog.list_templates() == catalog.Catalog((), ())


# --------------------------------------------------------------------------
# part 4: the instance
# --------------------------------------------------------------------------


def test_a_crewmate_with_no_dashboard_reads_empty(one_builtin):
    record = instance.read("fleet")
    assert record.state == instance.STATE_EMPTY
    assert record.instance_version == 0
    assert record.wire()["template"] == {"id": "", "version": 0}


def test_adopt_copies_the_template_and_starts_at_version_one(one_builtin):
    record = instance.adopt("fleet", "fixture-board")
    assert record.instance_version == 1
    assert record.template_id == "fixture-board" and record.template_version == 1
    assert record.html == PAGE
    assert record.state == instance.STATE_LIVE
    assert set(record.manifest["fields"]) == {"credits", "phase"}


def test_the_wire_shape_is_the_one_the_frame_reads(one_builtin):
    body = instance.adopt("fleet", "fixture-board").wire()
    assert set(body) >= {"instance_version", "template", "html", "manifest", "state"}
    assert set(body["template"]) == {"id", "version"}


def test_adopting_an_unknown_template_is_refused(one_builtin):
    with pytest.raises(instance.InstanceRefused, match="no dashboard template"):
        instance.adopt("fleet", "nope")


def test_an_edit_bumps_the_instance_version_and_not_the_templates(one_builtin):
    instance.adopt("fleet", "fixture-board")
    record = instance.edit("fleet", html=PAGE_EDITED)
    assert record.instance_version == 2
    assert record.html == PAGE_EDITED
    # The template version is FROZEN at adopt: editing a copy does not make the
    # crewmate the template's author.
    assert record.template_version == 1


def test_an_edit_before_adopting_is_refused(one_builtin):
    with pytest.raises(instance.InstanceRefused, match="adopt a template first"):
        instance.edit("fleet", html=PAGE_EDITED)


def test_an_empty_edit_is_refused(one_builtin):
    instance.adopt("fleet", "fixture-board")
    with pytest.raises(instance.InstanceRefused, match="must change"):
        instance.edit("fleet")


def test_a_page_binding_a_field_the_manifest_omits_never_becomes_a_version(one_builtin):
    instance.adopt("fleet", "fixture-board")
    with pytest.raises(instance.InstanceRefused, match="does not declare"):
        instance.edit("fleet", html=PAGE + '<u data-dashboard-field="ghost"></u>')
    # Refused, not stored: the version did not move.
    assert instance.read("fleet").instance_version == 1


def test_a_manifest_declaring_a_field_the_page_never_binds_is_refused(one_builtin):
    instance.adopt("fleet", "fixture-board")
    extra = _manifest()
    extra["fields"]["unseen"] = {"type": "number", "source": {"fold": "usage", "path": "x"}}
    with pytest.raises(instance.InstanceRefused, match="never binds"):
        instance.edit("fleet", manifest=extra)


def test_an_oversized_page_is_refused(one_builtin):
    instance.adopt("fleet", "fixture-board")
    big = PAGE + "<!--" + "x" * instance.MAX_INSTANCE_HTML_BYTES + "-->"
    with pytest.raises(instance.InstanceRefused, match="ceiling"):
        instance.edit("fleet", html=big)


def test_a_rollback_moves_the_version_forward(one_builtin):
    instance.adopt("fleet", "fixture-board")
    instance.edit("fleet", html=PAGE_EDITED)
    record = instance.rollback("fleet", 1)
    # Version THREE holding version one's page. Rewinding the counter instead would
    # make two different pages both "version 2".
    assert record.instance_version == 3
    assert record.html == PAGE


def test_a_rollback_to_the_current_or_a_future_version_is_refused(one_builtin):
    instance.adopt("fleet", "fixture-board")
    with pytest.raises(instance.InstanceRefused, match="already the current"):
        instance.rollback("fleet", 1)
    with pytest.raises(instance.InstanceRefused, match="does not exist"):
        instance.rollback("fleet", 7)


def test_a_rollback_to_a_version_no_longer_kept_says_what_is_kept(one_builtin):
    instance.adopt("fleet", "fixture-board")
    for _ in range(instance.MAX_RETAINED_VERSIONS + 1):
        instance.edit("fleet", html=PAGE_EDITED)
        instance.edit("fleet", html=PAGE)
    kept = instance.versions("fleet")
    assert len(kept) == instance.MAX_RETAINED_VERSIONS
    with pytest.raises(instance.InstanceRefused, match="no longer kept"):
        instance.rollback("fleet", 1)


def test_a_copy_of_a_template_that_shipped_a_new_version_reads_stale(one_builtin, _home):
    instance.adopt("fleet", "fixture-board")
    _write_template(_home / "fixture-board", _manifest(version=2))
    record = instance.read("fleet")
    assert record.state == instance.STATE_STALE
    assert "version 2" in record.state_reason


def test_a_copy_whose_template_left_the_registry_reads_stale(one_builtin, _home):
    instance.adopt("fleet", "fixture-board")
    for path in (_home / "fixture-board").iterdir():
        path.unlink()
    (_home / "fixture-board").rmdir()
    record = instance.read("fleet")
    assert record.state == instance.STATE_STALE
    assert "no longer in the registry" in record.state_reason


def test_a_damaged_record_reads_error_rather_than_empty(one_builtin):
    instance.adopt("fleet", "fixture-board")
    path = instance.instance_dir("fleet") / "instance.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["html"] = "<div>nothing bound here</div>"
    path.write_text(json.dumps(raw), encoding="utf-8")
    record = instance.read("fleet")
    # ERROR, not EMPTY. The two need opposite answers from a human: one is a fresh
    # crewmate, the other is a dashboard that stopped working.
    assert record.state == instance.STATE_ERROR
    assert "no longer loads" in record.state_reason


def test_a_record_from_another_schema_is_not_interpreted(one_builtin):
    instance.adopt("fleet", "fixture-board")
    path = instance.instance_dir("fleet") / "instance.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema"] = instance.SCHEMA_VERSION + 1
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert instance.read("fleet").state == instance.STATE_ERROR


def test_two_crewmates_dashboards_do_not_touch(one_builtin):
    instance.adopt("fleet", "fixture-board")
    instance.edit("fleet", html=PAGE_EDITED)
    instance.adopt("scout", "fixture-board")
    assert instance.read("fleet").instance_version == 2
    assert instance.read("scout").instance_version == 1
    assert instance.read("scout").html == PAGE


# --------------------------------------------------------------------------
# part 4: the history fold
# --------------------------------------------------------------------------


def test_every_change_lands_one_history_row_in_order(one_builtin):
    instance.adopt("fleet", "fixture-board")
    instance.edit("fleet", html=PAGE_EDITED)
    instance.rollback("fleet", 1)
    rows = instance.history("fleet")
    assert [r["action"] for r in rows] == ["adopted", "edited", "rolled_back"]
    assert [r["instance_version"] for r in rows] == [1, 2, 3]
    # A rollback records WHICH version it restored, which is what makes the row
    # readable without the payload beside it.
    assert rows[-1]["from_version"] == 1


def test_a_history_row_carries_what_changed_and_never_the_page(one_builtin):
    instance.adopt("fleet", "fixture-board")
    (row,) = instance.history("fleet")
    assert row["html_bytes"] == len(PAGE.encode("utf-8"))
    assert "html" not in row and "manifest" not in row
    assert row["fields"] == 2


def test_the_history_is_bounded(one_builtin):
    instance.adopt("fleet", "fixture-board")
    for _ in range(instance.MAX_HISTORY_ROWS + 5):
        instance.edit("fleet", html=PAGE_EDITED)
        instance.edit("fleet", html=PAGE)
    rows = instance.history("fleet")
    assert len(rows) == instance.MAX_HISTORY_ROWS
    # Newest LAST, so a reader showing the latest change reads the end.
    assert rows[-1]["instance_version"] > rows[0]["instance_version"]


def test_a_refused_change_leaves_no_history_row(one_builtin):
    instance.adopt("fleet", "fixture-board")
    with pytest.raises(instance.InstanceRefused):
        instance.edit("fleet", html="<div>unbound</div>")
    assert len(instance.history("fleet")) == 1


def test_the_history_rebuilds_when_its_savepoint_is_gone(one_builtin):
    instance.adopt("fleet", "fixture-board")
    instance.edit("fleet", html=PAGE_EDITED)
    (instance.instance_dir("fleet") / "history.json").unlink()
    # No session to refold from, so the answer is empty rather than wrong: the
    # savepoint is a cache of the log, and with neither there is nothing to report.
    assert instance.history("fleet") == ()
    # And it does not raise, which is the property that matters -- the history is not
    # the record, so losing it must not cost the dashboard.
    assert instance.read("fleet").instance_version == 2


def test_the_history_entry_type_is_the_one_the_crew_log_declares():
    from kiro_crew.crew_log.entry_types import SESSION_ENTRY_TYPES

    assert instance.ENTRY_TYPE in SESSION_ENTRY_TYPES
    declared = SESSION_ENTRY_TYPES[instance.ENTRY_TYPE]
    action = next(f for f in declared.fields if f.name == "action")
    # One tuple, two readers. A fourth action written here and refused there would be
    # a change the store reports as taken and the log drops.
    assert set(action.enum) == set(instance.ACTIONS)
    assert action.enum_closed


# --------------------------------------------------------------------------
# part 7: share
# --------------------------------------------------------------------------


def test_a_template_round_trips_through_one_file(one_builtin):
    text = share.export_template("fixture-board")
    imported = share.import_template(text, as_id="borrowed")
    assert imported.html == PAGE
    assert imported.id == "borrowed"
    # Shared, not builtin: the receiving registry must not claim the product shipped it.
    assert imported.manifest.source == catalog.SHARED_SOURCE
    assert imported.origin == catalog.USER_SOURCE


def test_an_import_that_collides_is_refused_and_names_the_collision(one_builtin):
    text = share.export_template("fixture-board")
    with pytest.raises(share.ShareRefused, match="builtin template already uses"):
        share.import_template(text)
    assert catalog.list_templates().by_id["fixture-board"].origin == catalog.BUILTIN_SOURCE


def test_a_malformed_share_never_becomes_a_directory(one_builtin):
    text = share.export_template("fixture-board")
    document = json.loads(text)
    document["manifest"]["fields"]["ghost"] = {
        "type": "number",
        "source": {"fold": "usage", "path": "x"},
    }
    with pytest.raises(share.ShareRefused, match="does not load"):
        share.import_template(json.dumps(document), as_id="ghosted")
    assert "ghosted" not in catalog.list_templates().by_id
    assert not (catalog.user_dir() / "ghosted").exists()


@pytest.mark.parametrize(
    "mangle, needle",
    [
        (lambda d: d.update(format="something.else"), "is not"),
        (lambda d: d.update(format_version=99), "format version"),
        (lambda d: d.pop("manifest"), "no manifest"),
        (lambda d: d.update(html=""), "no page"),
    ],
)
def test_an_import_refuses_a_document_it_cannot_trust(one_builtin, mangle, needle):
    document = json.loads(share.export_template("fixture-board"))
    mangle(document)
    with pytest.raises(share.ShareRefused, match=needle):
        share.import_template(json.dumps(document))


def test_two_imports_of_one_unused_id_do_not_overwrite_each_other(one_builtin):
    """The second import is refused by the directory it cannot create.

    Both see the id free: the collision check reads a registry scan, and the second
    scan is taken before the first import's files land. The claim is what separates
    them, so the template the first import had accepted is the one still served.
    """
    text = share.export_template("fixture-board")
    assert share.import_template(text, as_id="borrowed").html == PAGE
    live = catalog.list_templates
    reads: list[int] = []

    def stale_then_live() -> catalog.Catalog:
        reads.append(1)
        return catalog.Catalog((), ()) if len(reads) == 1 else live()

    document = json.loads(text)
    document["html"] = PAGE_EDITED
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(catalog, "list_templates", stale_then_live)
        with pytest.raises(share.ShareRefused, match="another import holds the id"):
            share.import_template(json.dumps(document), as_id="borrowed")
    assert catalog.list_templates().by_id["borrowed"].html == PAGE


def test_a_failure_after_the_claim_hands_the_id_back(one_builtin):
    """A claim an import cannot finish leaves no directory, so the id stays importable."""
    text = share.export_template("fixture-board")

    def unwritable(*args: object, **kwargs: object) -> None:
        raise OSError("no space left on device")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(share, "atomic_write", unwritable)
        with pytest.raises(OSError, match="no space left"):
            share.import_template(text, as_id="borrowed")
    assert not (catalog.user_dir() / "borrowed").exists()
    assert share.import_template(text, as_id="borrowed").html == PAGE


def test_an_import_refuses_text_that_is_not_json(one_builtin):
    with pytest.raises(share.ShareRefused, match="not a JSON document"):
        share.import_template("{{{")


def test_an_oversized_share_is_refused(one_builtin):
    with pytest.raises(share.ShareRefused, match="ceiling"):
        share.import_template(" " * (share.MAX_SHARE_BYTES + 1))


def test_an_imported_template_is_stored_and_listed_but_cannot_be_adopted(one_builtin):
    """Import keeps the file; adopt refuses it. The exfiltration path ends here.

    A rendered page runs its own inline script against this crewmate's fold values, so
    adopting a page an arbitrary sender wrote would hand task titles and summaries to
    that page. Import still works -- the file is stored and listed, which is what P2's
    picker reads -- and only becoming the LIVE dashboard is refused.
    """
    share.import_template(share.export_template("fixture-board"), as_id="borrowed")
    assert "borrowed" in {entry.id for entry in catalog.list_templates().entries}
    with pytest.raises(instance.InstanceRefused, match="cannot be adopted"):
        instance.adopt("fleet", "borrowed")


def test_a_user_written_template_can_still_be_adopted(one_builtin):
    """The other side of the provenance rule, so it cannot pass by refusing everything."""
    _write_template(catalog.user_dir() / "mine", _manifest(id="mine", source="user"))
    record = instance.adopt("fleet", "mine")
    assert record.template_id == "mine" and record.state == instance.STATE_LIVE


def test_the_render_path_refuses_an_imported_page_already_on_disk(one_builtin):
    """Defense in depth: the record that got past adopt is still not rendered.

    A record written before the adopt refusal existed, or hand-edited on disk, reaches
    the composer without passing that check. ``None`` is the tab's unavailable state.
    """
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers import member_dashboard

    record = instance.adopt("fleet", "fixture-board")
    hand_edited = dict(record.manifest)
    hand_edited["source"] = catalog.SHARED_SOURCE
    poisoned = SimpleNamespace(manifest=hand_edited, html=record.html)
    assert member_dashboard._render("fleet", "fleet", poisoned) is None


# --------------------------------------------------------------------------
# part 7: snapshots
# --------------------------------------------------------------------------


def _take(slug="fleet", **over):
    args = {
        "template_id": "fixture-board",
        "template_version": 1,
        "instance_version": 1,
        "fields": {"credits": 12.5, "phase": "driving"},
        "seq": 418,
    }
    args.update(over)
    return snapshot.take_snapshot(slug, **args)


def test_a_snapshot_keeps_all_five_parts(one_builtin):
    taken = _take()
    read_back = snapshot.read_snapshot("fleet", taken.id)
    assert read_back.template_id == "fixture-board"
    assert read_back.template_version == 1
    assert read_back.instance_version == 1
    assert read_back.fields == {"credits": 12.5, "phase": "driving"}
    assert read_back.seq == 418
    assert read_back.captured_ms > 0


@pytest.mark.parametrize(
    "missing",
    [
        {"template_id": ""},
        {"template_version": 0},
        {"instance_version": 0},
        {"fields": {}},
        {"seq": -1},
    ],
)
def test_a_snapshot_missing_a_part_is_refused(one_builtin, missing):
    with pytest.raises(snapshot.SnapshotRefused):
        _take(**missing)


def test_snapshots_are_listed_oldest_first_and_bounded(one_builtin):
    for n in range(snapshot.MAX_SNAPSHOTS + 3):
        _take(seq=n)
    ids = snapshot.list_snapshots("fleet")
    assert len(ids) == snapshot.MAX_SNAPSHOTS
    assert list(ids) == sorted(ids)


def test_a_snapshot_id_that_is_not_one_never_reaches_a_path(one_builtin):
    with pytest.raises(snapshot.SnapshotRefused, match="is not a snapshot id"):
        snapshot.read_snapshot("fleet", "../../../etc/passwd")


def test_an_unknown_snapshot_is_refused_by_id(one_builtin):
    with pytest.raises(snapshot.SnapshotRefused, match="no snapshot"):
        snapshot.read_snapshot("fleet", "1759490000000-deadbeef")


def test_an_oversized_snapshot_is_refused(one_builtin):
    with pytest.raises(snapshot.SnapshotRefused, match="ceiling"):
        _take(fields={"credits": "x" * (snapshot.MAX_SNAPSHOT_BYTES + 1)})


def test_two_crewmates_snapshots_do_not_mix(one_builtin):
    mine = _take("fleet")
    _take("scout")
    assert snapshot.list_snapshots("fleet") == (mine.id,)
    with pytest.raises(snapshot.SnapshotRefused, match="no snapshot"):
        snapshot.read_snapshot("scout", mine.id)


def test_no_grammar_in_these_modules_anchors_with_a_dollar():
    """The CLASS, pinned once, rather than each pattern as it is found.

    Python's ``$`` also matches just before a trailing newline, so a ``$``-anchored
    grammar accepts ``"<valid>\\n"``. Every pattern here guards something that becomes a
    filename, a directory name, a registry key or a DOM attribute, and the same hole was
    found twice in this change -- once on a template id and once on a snapshot id. A
    per-pattern test would have passed for the one not yet looked at, so the rule is
    held over the modules as a whole: an anchored grammar ends with ``\\Z``.
    """
    import re as _re

    checked = []
    for module in (catalog, instance, manifest_mod, share, snapshot):
        for name, value in vars(module).items():
            if not isinstance(value, _re.Pattern):
                continue
            pattern = value.pattern
            if not pattern.startswith("^"):
                continue  # not an anchored grammar; nothing claims a whole-string match
            checked.append(f"{module.__name__}.{name}")
            assert not pattern.endswith("$"), (
                f"{module.__name__}.{name} anchors with '$', so it accepts a trailing "
                f"newline: {pattern!r}. Use r'...\\Z'."
            )
    # Belt and braces: if the walk finds nothing, the assertion above is vacuous and
    # this test would pass while the rule went unchecked.
    assert len(checked) >= 4, f"the walk found only {checked}, so it is not covering them"


def test_a_snapshot_id_with_a_trailing_newline_never_reaches_the_path_join():
    """``read_snapshot`` says no caller-supplied text reaches the join. Held to it.

    The refusal must be the GRAMMAR's, not a later file miss: both answer
    ``SnapshotRefused``, so a test that only checked the exception would pass either way
    and the stated guarantee would go unchecked.
    """
    with pytest.raises(snapshot.SnapshotRefused, match="is not a snapshot id"):
        snapshot.read_snapshot("fleet", "1759490000000-deadbeef\n")
