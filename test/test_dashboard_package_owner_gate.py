"""A dashboard package is written by the crewmate it is bound to, and by nobody else.

The split this protects is the one that makes a free-HTML page safe to run at all: the
owner composes the page and a person approves it, workers only `work_report` and the fold
carries their reports to it. A worker that could write the package could write the page --
and the page runs script inside the frame that holds the owner's own fold values.

The refusal is a SENTENCE because the party that reads it is an agent that can act on it:
whose dashboard this is, and what to do instead. A code would be rendered to a worker as
`owner_only` with nothing to do next.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from body_stream_helpers import JsonBodyRequest

from kiro_crew.artifact_store import dashboard_package as dp


def _install_roster(monkeypatch: pytest.MonkeyPatch, slugs: dict[str, str]) -> None:
    """Patch the roster to hold exactly *slugs*, keys and all.

    The gate asks the roster TWO things now -- this caller's own key, and how many
    keys the binding's slug names -- so a fixture that answered only the first would
    leave the ambiguity count reading the developer's real config.
    """
    from types import SimpleNamespace

    import kiro_crew.members as members_mod
    from kiro_crew.config import loader as loader_mod

    def member_slug(name: str, config: Any = None) -> str:
        if name not in slugs:
            raise members_mod.MemberSlugError(name)
        return slugs[name]

    monkeypatch.setattr(members_mod, "member_slug", member_slug)
    monkeypatch.setattr(
        loader_mod.KiroCrewConfig,
        "load",
        staticmethod(lambda *a, **k: SimpleNamespace(agents=dict.fromkeys(slugs, None))),
    )


@pytest.fixture()
def roster(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slugs for two crews, resolved the way the roster resolves them."""
    _install_roster(monkeypatch, {"Atlas": "atlas", "Nyx": "nyx"})


class TestACrewmateBinding:
    def test_the_owner_may_write_its_own_package(self, roster: None) -> None:
        assert dp.binding_refusal("crewmate:atlas", crew="Atlas") == ""

    def test_a_worker_is_refused_and_told_what_to_do(self, roster: None) -> None:
        said = dp.binding_refusal("crewmate:atlas", crew="Nyx")
        assert "atlas" in said
        assert "Nyx" in said
        assert "work_report" in said, "the refusal does not say what the worker should do"

    def test_a_caller_with_no_crew_is_refused(self, roster: None) -> None:
        """A gate whose default is "no identity, no objection" is not a gate."""
        assert dp.binding_refusal("crewmate:atlas") != ""

    def test_a_caller_whose_name_has_no_slug_is_refused(self, roster: None) -> None:
        """An unslugifiable caller cannot be shown to own anything, which is the same
        answer as a mismatch rather than a crash on the write path."""
        assert dp.binding_refusal("crewmate:atlas", crew="Ghost") != ""

    def test_the_slug_is_compared_not_the_display_name(self, roster: None) -> None:
        """Slugification is lossy, so two configured names can reach one slug. Comparing
        names would let each pass for the other's page; comparing slugs is what the
        binding actually stores."""
        assert dp.binding_refusal("crewmate:atlas", crew="atlas") != ""

    def test_a_slug_two_crews_resolve_to_authorizes_neither(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A roster holding both `Atlas` and `atlas` derives ONE slug from two names, so
        the comparison below would authorize whichever of them asked. Neither can be
        shown to own the page, so both are refused and the remedy is a rename."""
        _install_roster(monkeypatch, {"Atlas": "atlas", "atlas": "atlas", "Nyx": "nyx"})
        for caller in ("Atlas", "atlas"):
            said = dp.binding_refusal("crewmate:atlas", crew=caller)
            assert said != "", f"{caller!r} was authorized for an ambiguous slug"
            assert "dashboard_slug_ambiguous" in said, said

    def test_an_unambiguous_owner_is_unaffected_by_the_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The boundary is TWO owners, not "the roster was asked": one owner still
        writes its own page."""
        _install_roster(monkeypatch, {"Atlas": "atlas", "Nyx": "nyx"})
        assert dp.binding_refusal("crewmate:atlas", crew="Atlas") == ""


class TestASessionBinding:
    def test_the_bound_session_may_write(self) -> None:
        assert dp.binding_refusal("session:chat-7-17910", slot="chat-7-17910") == ""

    def test_another_session_is_refused(self) -> None:
        said = dp.binding_refusal("session:chat-7-17910", slot="chat-9-17911")
        assert "chat-7-17910" in said
        assert "work_report" in said

    def test_a_caller_with_no_session_is_refused(self) -> None:
        assert dp.binding_refusal("session:chat-7-17910") != ""

    def test_a_crew_identity_does_not_satisfy_a_session_binding(self, roster: None) -> None:
        """The two scopes name different things. A crewmate that happened to pass here
        would be writing a page bound to a slot it does not own."""
        assert dp.binding_refusal("session:chat-7-17910", crew="Atlas") != ""


class TestAMalformedBinding:
    def test_a_binding_that_is_not_one_is_refused_with_its_own_reason(self) -> None:
        """Refused as a malformed binding rather than as a wrong owner: an author that
        wrote the grammar wrong needs the grammar, not a sentence about ownership."""
        for bad in ("atlas", "", None, 7, "crewmate:", "session:dashboard:chat-7"):
            said = dp.binding_refusal(bad, crew="Atlas", slot="chat-7-17910")
            assert said != ""
            assert "work_report" not in said, f"{bad!r} was answered as an ownership refusal"


# --------------------------------------------------------------------------- #
# Two ways the gate was skippable. Both were reported by review on
# e1f2ce06cb and both are authorization bypasses, so each gets a test that
# fails without the fix rather than a comment saying it is handled.
# --------------------------------------------------------------------------- #


class _Req:
    """The three things the gate reads off a request, and nothing else.

    ``get`` answers ``None`` for every key: an MCP caller carries no app claim, which
    is what distinguishes it from the app-token requests in
    :class:`TestAnAppTokenCannotWriteAPackage`.
    """

    def __init__(self, secret: bool = True) -> None:
        self.headers = {"X-Internal-Secret": "s"} if secret else {}

    def get(self, key: str, default: Any = None) -> Any:
        return default


@pytest.fixture()
def worker(monkeypatch: pytest.MonkeyPatch, roster: None) -> None:
    """A caller that is the crew ``Nyx`` -- not ``atlas``, whose pages these are."""
    from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

    monkeypatch.setattr(mdf, "writing_caller", lambda request: ("Nyx", "chat-9-17911"))


def _package_text(bound_to: str = "crewmate:atlas", kind: str = '"dashboard"') -> str:
    """A valid package as submitted BYTES, so a test can spell ``kind`` its own way."""
    return (
        "{"
        f'"kind":{kind},'
        f'"bound_to":"{bound_to}",'
        '"model":{"types":{"goal":{"type":"text",'
        '"source":{"fold":"work","path":"conductor.goal"}}}},'
        # A page that BINDS NOTHING, drawing from `subscribe` instead. The binding
        # attribute is itself spelled `data-dashboard-field`, so a page that used one
        # would put the word in the bytes and the old substring check would have caught
        # this payload by accident -- which is not the payload an attacker sends.
        '"view":{"html":"<canvas id=t></canvas>"},'
        '"theme":{"tokens":{}}'
        "}"
    )


class TestTheKindCannotBeHiddenFromTheGate:
    """The gate classified the content by looking for the word ``dashboard`` in the
    submitted bytes. JSON can spell any character as an escape, so the word need never
    appear -- and the store's own ``json.loads`` decodes it to a package regardless.

    Reported independently by two review lanes, which is the signal that it is reachable
    rather than theoretical: the worker needs no special access, only a text editor.
    """

    def test_a_package_whose_kind_is_escaped_is_still_gated(self, worker: None) -> None:
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        # No literal "dashboard" anywhere in these bytes.
        text = _package_text(kind='"dash\\u0062oard"')
        assert "dashboard" not in text
        said = mdf.package_write_refusal(_Req(), text)
        assert said, "a worker can write another crewmate's page by escaping one letter"
        assert "atlas" in said

    def test_every_letter_escaped_is_still_gated(self, worker: None) -> None:
        """The fix cannot be a longer list of spellings to look for."""
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        escaped = "".join(f"\\u{ord(c):04x}" for c in "dashboard")
        assert mdf.package_write_refusal(_Req(), _package_text(kind=f'"{escaped}"'))

    def test_a_prose_document_is_still_not_gated(self, worker: None) -> None:
        """The cheap pre-filter has to stay cheap AND unescapable: a JSON document
        begins with '{' and no escape hides that, so prose never reaches a parse."""
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        for text in ("# just a note", "dashboard is a word I used in prose", "", None):
            assert mdf.package_write_refusal(_Req(), text) == ""

    def test_json_that_is_not_a_package_is_not_gated(self, worker: None) -> None:
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        assert mdf.package_write_refusal(_Req(), '{"kind":"note","body":"hi"}') == ""


class TestTheStoredBindingIsAuthorizedToo:
    """The gate read only the binding being WRITTEN, which is a claim the caller wrote.

    So a worker submitted a package bound to ITSELF, passed, and overwrote the slug
    holding another crewmate's page -- rebinding that crewmate's dashboard to the worker
    in one write. The binding that decides whether a slug may be written is the one the
    slug holds now.
    """

    def test_a_worker_cannot_overwrite_another_crewmates_slug_by_rebinding_it(
        self, worker: None
    ) -> None:
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        said = mdf.package_write_refusal(
            _Req(),
            # Bound to the worker itself: authorizing this alone always passes.
            _package_text(bound_to="crewmate:nyx"),
            # What the slug holds now.
            _package_text(bound_to="crewmate:atlas"),
        )
        assert said, "a worker can take over another crewmate's dashboard by rebinding it"
        assert "atlas" in said

    def test_a_worker_cannot_blank_another_crewmates_page_with_prose(self, worker: None) -> None:
        """The submitted content is not a package at all, so classifying only the
        submission answers "not mine to judge" and the store overwrites the page."""
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        said = mdf.package_write_refusal(_Req(), "# gone", _package_text("crewmate:atlas"))
        assert said
        assert "atlas" in said

    def test_the_owner_may_still_rewrite_its_own_page(self, worker: None) -> None:
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        monkeypatched = mdf.package_write_refusal(
            _Req(), _package_text("crewmate:nyx"), _package_text("crewmate:nyx")
        )
        assert monkeypatched == ""

    def test_a_browser_caller_is_not_gated_here(self) -> None:
        """No internal secret: the person at the dashboard, already owner-gated by the
        artifact routes. Asserted so the fix above cannot quietly start refusing them."""
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        assert (
            mdf.package_write_refusal(
                _Req(secret=False), _package_text(), _package_text("crewmate:atlas")
            )
            == ""
        )


class TestAnAppTokenCannotWriteAPackage:
    """An app token carries no internal secret, so the person's exemption must not be
    reachable by one.

    Every condition is ordinary rather than exotic, which is why this is a test and not
    a note: app-token requests carry `request["app"]`, the artifact routes' own
    `_non_owner_refusal` returns early for exactly that claim, and shipped apps already
    declare `/api/artifacts` grants and write through that route. So nothing between the
    token and the store asked whose page it was.
    """

    class _AppReq:
        """An app-token request: an app claim, and no internal secret."""

        def __init__(self, app: str = "code-review-sage") -> None:
            self.headers: dict[str, str] = {}
            self._store = {"app": app}

        def get(self, key: str, default: Any = None) -> Any:
            return self._store.get(key, default)

    def test_an_app_is_refused_even_for_a_package_bound_to_nobody_it_knows(
        self, roster: None
    ) -> None:
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        said = mdf.package_write_refusal(self._AppReq(), _package_text("crewmate:atlas"))
        assert said, "an app token can overwrite a mate's dashboard package"
        assert "app" in said.lower()

    def test_an_app_cannot_rebind_a_stored_package_to_itself(self, roster: None) -> None:
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        said = mdf.package_write_refusal(
            self._AppReq(),
            _package_text("crewmate:nyx"),
            _package_text("crewmate:atlas"),
        )
        assert said

    def test_an_app_writing_a_prose_document_is_untouched(self, roster: None) -> None:
        """The gate is asked on every artifact write. An app saving a note is its own
        business, and refusing that would break every app that uses the library."""
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        assert mdf.package_write_refusal(self._AppReq(), "# a note") == ""

    def test_the_person_at_the_dashboard_is_still_exempt(self, roster: None) -> None:
        """No secret AND no app claim: the browser session the artifact routes have
        already owner-gated. The app check above is what makes that a positive
        identification rather than "whoever sent no secret"."""
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        class _Person:
            headers: dict[str, str] = {}

            def get(self, key: str, default: Any = None) -> Any:
                return default

        assert mdf.package_write_refusal(_Person(), _package_text("crewmate:atlas")) == ""


class TestTheWriteLandsOnTheSNAPSHOTTheGateJudged:
    """The gate reads the stored binding, and the write happens later.

    Two moments, so a rebind arriving between them is the hazard: an authorization made
    against the binding the slug held at the read, committed by a store whose default is
    last-write-wins, puts the caller's page on the crewmate who just took the slug --
    and nothing puts the owner's binding back. An agent holding several PATCHes open
    chooses that timing itself.

    What closes it is carrying the token of the read the gate judged into the write, so
    the store refuses a write based on a snapshot that has moved on.
    """

    def test_the_store_refuses_the_write_when_the_record_moved_under_it(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE RACE ITSELF, against the real store and with no threads: the rebind is
        simply performed between the read and the write, which is the interleaving the
        agent is choosing."""
        from kiro_crew import artifacts as art_mod
        from kiro_crew.artifacts import ArtifactConflictError, ArtifactStore
        from kiro_crew.dashboard.handlers.artifacts import _stored_snapshot_for

        store = ArtifactStore(root=tmp_path / "artifacts")
        made = store.create(name="page", content=_package_text("crewmate:atlas"), kind="dashboard")
        monkeypatch.setattr(art_mod, "_default_store", store)

        # 1. What the gate reads and judges.
        content, token, snapshot = _stored_snapshot_for(made.slug)
        assert snapshot == "found"
        assert content is not None and token, "the gate has no snapshot to authorize"
        assert "atlas" in content

        # 2. The owner rebinds the slug to another crewmate while that write is open,
        #    under its own read's token -- which the store now requires of every write
        #    that lands on a dashboard, so this is also what the route does.
        store.update(
            made.slug,
            content=_package_text("crewmate:nyx"),
            snapshot_token=store.get(made.slug, assign_token=True).content_token,
        )

        # 3. The write the gate authorized now lands -- and must not.
        with pytest.raises(ArtifactConflictError):
            store.update(made.slug, content=_package_text("crewmate:atlas"), expected_token=token)
        assert "nyx" in (store.get(made.slug).content or ""), "the rebind was overwritten"

    @pytest.mark.asyncio
    async def test_the_update_route_writes_under_the_token_it_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """And the route is what must carry it. A caller that sends no
        ``expected_token`` -- every agent, since the field is optional -- still has the
        gate's own read named for it, under ``snapshot_token``: the store needs the
        read this write was authorized against, and the caller's optional field is a
        different question it may not have answered."""
        wrote = await _update_through_the_route(
            monkeypatch, stored=_package_text("crewmate:atlas"), submitted=_package_text()
        )
        assert wrote["snapshot_token"] == "token-of-the-read", (
            "the route wrote without the token of the snapshot it authorized, so a "
            "rebind landing in between is overwritten"
        )

    @pytest.mark.asyncio
    async def test_a_callers_own_token_is_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A caller that sends one is re-basing on a read of its own. A stale token
        fails the store's check anyway, so keeping it cannot widen the write."""
        wrote = await _update_through_the_route(
            monkeypatch,
            stored=_package_text("crewmate:atlas"),
            submitted=_package_text(),
            expected_token="token-the-caller-read",
        )
        assert wrote["expected_token"] == "token-the-caller-read"

    @pytest.mark.asyncio
    async def test_an_ordinary_document_keeps_last_write_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Asked on EVERY artifact write. Forcing the check on a prose document would
        turn the editor's autosave into a conflict the person never caused."""
        wrote = await _update_through_the_route(
            monkeypatch, stored="# yesterday's note", submitted="# today's note"
        )
        assert wrote["expected_token"] is None


class TestTheDedupPathReadsTheRecordItLandsOn:
    """A save carrying a ``source_path`` lands on whatever artifact already points at
    that path, and that record is found by SCANNING META -- so it carries no content.

    Handing that to the gate is the empty-snapshot hole wearing another hat: with no
    stored binding to check, the gate judges only the binding the caller submitted,
    which the caller wrote. A member who copies another member's source path and
    re-saves it through this route would take their page.
    """

    @pytest.mark.asyncio
    async def test_a_worker_re_saving_another_crewmates_source_path_is_refused(
        self, worker: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        status, reached = await _save_onto_a_deduped_record(
            monkeypatch,
            stored=_package_text("crewmate:atlas"),
            submitted=_package_text("crewmate:nyx"),
        )
        assert status == 403, "a worker took another crewmate's page through the dedup path"
        assert not reached, "the write reached the store anyway"

    @pytest.mark.asyncio
    async def test_the_owner_may_still_re_save_its_own(
        self, roster: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate has to read the record, not refuse everything that reaches it."""
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        monkeypatch.setattr(mdf, "writing_caller", lambda request: ("Atlas", "chat-1-1790"))
        status, reached = await _save_onto_a_deduped_record(
            monkeypatch,
            stored=_package_text("crewmate:atlas"),
            submitted=_package_text("crewmate:atlas"),
        )
        assert reached, "the owner was refused its own page"
        assert status == 0

    @pytest.mark.asyncio
    async def test_a_snapshot_that_is_gone_by_the_read_refuses_the_write(
        self, worker: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The source-path lookup found a record and the read by slug found none, which
        is the window between them: the target deleted, and its slug free to be
        recreated as somebody else's dashboard before this write lands.

        There is no snapshot to authorize and no token to carry, so the write is
        refused rather than committed under the caller's own claim.
        """
        status, reached = await _save_onto_a_deduped_record(
            monkeypatch,
            stored="",
            submitted=_package_text("crewmate:nyx"),
            reader=_raises(_not_found()),
        )
        assert status == 409, "a package write went out with no snapshot behind it"
        assert not reached, "the write reached the store with no token"

    @pytest.mark.asyncio
    async def test_a_tokenless_snapshot_refuses_the_write_too(
        self, roster: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A record that answers without a token is the same hole wearing a hat: the
        guard would be dropped silently and the write would be last-write-wins."""
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        monkeypatch.setattr(mdf, "writing_caller", lambda request: ("Atlas", "chat-1-1790"))
        status, reached = await _save_onto_a_deduped_record(
            monkeypatch,
            stored=_package_text("crewmate:atlas"),
            submitted=_package_text("crewmate:atlas"),
            reader=lambda slug, **_k: SimpleNamespace(
                slug=slug,
                kind="dashboard",
                content=_package_text("crewmate:atlas"),
                content_token="",
            ),
        )
        assert status == 409
        assert not reached

    @pytest.mark.asyncio
    async def test_an_ordinary_document_is_not_held_to_that(
        self, roster: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The requirement belongs to this gate, not to the artifact library: a note
        de-duped onto its own path is saved the way it is on base."""
        status, reached = await _save_onto_a_deduped_record(
            monkeypatch,
            stored="# yesterday",
            submitted="# today",
            reader=lambda slug, **_k: SimpleNamespace(
                slug=slug, kind="html", content="# yesterday", content_token=""
            ),
        )
        assert reached, "an ordinary de-duped save stopped reaching the store"
        assert status == 0

    @pytest.mark.asyncio
    async def test_the_write_goes_out_under_the_token_of_that_read(
        self, roster: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same reason as the update route: the gate judged a snapshot and this write
        lands later, so a rebind in between must meet the store's refusal."""
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        monkeypatch.setattr(mdf, "writing_caller", lambda request: ("Atlas", "chat-1-1790"))
        wrote: dict[str, Any] = {}
        await _save_onto_a_deduped_record(
            monkeypatch,
            stored=_package_text("crewmate:atlas"),
            submitted=_package_text("crewmate:atlas"),
            captured=wrote,
        )
        assert wrote.get("snapshot_token") == "token-of-the-read"


async def _save_onto_a_deduped_record(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stored: str,
    submitted: str,
    captured: dict[str, Any] | None = None,
    reader: Any = None,
) -> tuple[int, bool]:
    """Drive ``api_artifacts_create`` down the ``source_path`` dedup branch.

    The store answers the source-path lookup with a META-ONLY record, the way the real
    one does, and serves content only when asked by slug -- which is the distinction
    under test.
    """
    import json
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers import artifacts as handler

    seen: dict[str, Any] = captured if captured is not None else {}
    reached: list[bool] = []

    def _update(_slug: str, **kwargs: Any) -> Any:
        reached.append(True)
        seen.update(kwargs)
        raise _Wrote

    store = SimpleNamespace(
        find_by_source_path=lambda path: SimpleNamespace(
            slug="page", kind="dashboard", content=None, source_path=path
        ),
        get=reader
        or (
            lambda slug, **_k: SimpleNamespace(
                slug=slug, kind="dashboard", content=stored, content_token="token-of-the-read"
            )
        ),
        update=_update,
        list=lambda kind=None, **_k: [SimpleNamespace(slug="page", kind="dashboard")],
    )
    monkeypatch.setattr(handler, "get_default_store", lambda: store)
    monkeypatch.setattr(handler, "_is_restricted_session", lambda *_a: False)
    monkeypatch.setattr(handler, "_validate_inbound_webapp_metadata", lambda _b: "")

    async def _allowed(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(handler, "_non_owner_refusal", _allowed)

    # THE PROMOTE VERDICT, which is what decides whether a path is dedup-able at all
    # and canonicalizes it. Stubbed to "a linked path, these bytes" so this test is
    # about the gate rather than about path classification, which owns its own tests:
    # a path under the system temp dir is classified disposable and never reaches the
    # dedup branch, so a test that did not stub this would silently measure nothing.
    async def _verdict(_request: Any, _raw: str) -> Any:
        return "/repo/page.json", "/repo", False, ""

    async def _promoted(_path: str, _root: str) -> Any:
        return submitted, ""

    monkeypatch.setattr(handler, "_promote_verdict", _verdict)
    monkeypatch.setattr(handler, "_authoritative_promote_content", _promoted)

    class _Request(JsonBodyRequest):
        headers = {"X-Internal-Secret": "s", "X-Session-Key": "chat-9-17911"}
        match_info: dict[str, str] = {}
        query: dict[str, str] = {}
        app = {"state": SimpleNamespace()}

        def body_bytes(self) -> bytes:
            return json.dumps(
                {"name": "page", "content": submitted, "source_path": "/tmp/copied.json"}
            ).encode()

        def get(self, key: str, default: Any = None) -> Any:
            return default

    try:
        response = await handler.api_artifacts_create(_Request())  # type: ignore[arg-type]
    except _Wrote:
        return 0, True
    return int(response.status), bool(reached)


async def _create_under_a_fresh_slug(
    monkeypatch: pytest.MonkeyPatch,
    body_content: Any,
    promoted: str,
) -> tuple[int, bool]:
    """Drive ``api_artifacts_create`` down the NEW-SLUG branch.

    The source-path lookup finds nothing, so the dedup branch above is skipped and the
    create path is reached -- the third write path, where there is no stored record to
    compare against and the submitted bytes are all the gate has.

    ``body_content`` and ``promoted`` are kept separate on purpose: the request body and
    the file behind ``source_path`` are two different sources of bytes, and only the
    second is what gets stored when the first is absent.
    """
    import json
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers import artifacts as handler

    reached: list[bool] = []

    def _create(**_kwargs: Any) -> Any:
        reached.append(True)
        raise _Wrote

    store = SimpleNamespace(
        find_by_source_path=lambda _path: None,
        get=lambda slug, **_k: None,
        create=_create,
        list=lambda kind=None, **_k: [],
    )
    monkeypatch.setattr(handler, "get_default_store", lambda: store)
    monkeypatch.setattr(handler, "_is_restricted_session", lambda *_a: False)
    monkeypatch.setattr(handler, "_validate_inbound_webapp_metadata", lambda _b: "")

    async def _allowed(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(handler, "_non_owner_refusal", _allowed)

    async def _verdict(_request: Any, _raw: str) -> Any:
        return "/repo/page.json", "/repo", False, ""

    async def _promoted(_path: str, _root: str) -> Any:
        return promoted, ""

    monkeypatch.setattr(handler, "_promote_verdict", _verdict)
    monkeypatch.setattr(handler, "_authoritative_promote_content", _promoted)

    sent: dict[str, Any] = {"name": "page", "source_path": "/repo/page.json"}
    if body_content is not None:
        sent["content"] = body_content

    class _Request(JsonBodyRequest):
        headers = {"X-Internal-Secret": "s", "X-Session-Key": "chat-9-17911"}
        match_info: dict[str, str] = {}
        query: dict[str, str] = {}
        app = {"state": SimpleNamespace()}

        def body_bytes(self) -> bytes:
            return json.dumps(sent).encode()

        def get(self, key: str, default: Any = None) -> Any:
            return default

    try:
        response = await handler.api_artifacts_create(_Request())  # type: ignore[arg-type]
    except _Wrote:
        return 0, True
    return int(response.status), bool(reached)


class TestTheCreatePathGatesTheBytesItStores:
    """The new-slug branch stores the PROMOTED bytes when the body carries no content,
    so that is what the gate has to judge.

    Judging ``body["content"]`` alone is a privilege-class hole with no stored record to
    fall back on: an empty body parses as no package at all, the gate finds no binding
    to object to, and the file behind ``source_path`` is then written under whatever
    ``bound_to`` it declares. The route's binding resolver accepts a copy-only record,
    so the named crewmate's own tab would serve and run this caller's page beside that
    crewmate's fold values.
    """

    @pytest.mark.asyncio
    async def test_a_worker_creating_another_crewmates_package_from_a_path_is_refused(
        self, worker: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No ``content`` in the body at all -- the hole's own shape."""
        status, reached = await _create_under_a_fresh_slug(
            monkeypatch,
            body_content=None,
            promoted=_package_text("crewmate:atlas"),
        )
        assert status == 403, "a worker took a crewmate's page through the create path"
        assert not reached, "the write reached the store anyway"

    @pytest.mark.asyncio
    async def test_an_empty_body_content_does_not_hide_the_promoted_binding(
        self, worker: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``content: ""`` is the same hole spelled explicitly: it parses as no package,
        while the promoted bytes are still what lands."""
        status, reached = await _create_under_a_fresh_slug(
            monkeypatch,
            body_content="",
            promoted=_package_text("crewmate:atlas"),
        )
        assert status == 403, "an empty body content walked the promoted package past the gate"
        assert not reached, "the write reached the store anyway"

    @pytest.mark.asyncio
    async def test_the_owner_may_create_its_own_from_a_path(
        self, roster: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate has to read the promoted bytes, not refuse every path-backed create."""
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        monkeypatch.setattr(mdf, "writing_caller", lambda request: ("Atlas", "chat-1-1790"))
        status, reached = await _create_under_a_fresh_slug(
            monkeypatch,
            body_content=None,
            promoted=_package_text("crewmate:atlas"),
        )
        assert reached, "the owner was refused its own page"
        assert status == 0


class TestAReadThatFailedAuthorizesNothing:
    """A snapshot read has three outcomes, and two of them are not "nothing stored".

    Collapsing them is a privilege-class hole: with the stored content read as empty,
    the gate has only the binding the CALLER submitted -- a claim the caller wrote --
    and there is no token to carry into the write. So both guards are skipped at once,
    and the write lands. Two ways in: a transient read failure on an existing package,
    and an owner creating the slug between the read and the write.
    """

    @pytest.mark.asyncio
    async def test_a_read_that_raised_refuses_the_write_to_a_gated_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The submitted content is PROSE, so only the stored record says the gate owns
        this slug -- and it says so from the listing, which is metadata and readable
        when the record's own bytes are not."""
        status, reached = await _update_against_a_store(
            monkeypatch,
            reader=_raises(OSError("the artifact is unreadable")),
            submitted="# gone",
            listed=["page"],
        )
        assert status == 503, "an unreadable dashboard record was written over"
        assert not reached, "the write went to the store anyway"

    @pytest.mark.asyncio
    async def test_a_read_that_raised_refuses_a_submitted_package_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other way the gate owns the write: what is being submitted IS a package,
        whatever the slug holds now."""
        status, reached = await _update_against_a_store(
            monkeypatch,
            reader=_raises(OSError("the artifact is unreadable")),
            submitted=_package_text("crewmate:nyx"),
            listed=[],
        )
        assert status == 503
        assert not reached

    @pytest.mark.asyncio
    async def test_an_unreadable_record_of_another_kind_behaves_as_it_does_on_base(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE OTHER SIDE of the same question. This gate exists for dashboard packages,
        and a prose document whose bytes cannot be read is the store's own problem to
        report -- refusing it here would charge every other kind for this rule."""
        status, reached = await _update_against_a_store(
            monkeypatch,
            reader=_raises(OSError("the artifact is unreadable")),
            submitted="# today's note",
            listed=[],
        )
        assert reached, "an unreadable note stopped reaching the store"
        assert status == 0

    @pytest.mark.asyncio
    async def test_a_listing_that_also_failed_refuses_the_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail closed: nothing could say whose record this is, and a crewmate's
        dashboard is not written over on the strength of a failed lookup."""
        status, reached = await _update_against_a_store(
            monkeypatch,
            reader=_raises(OSError("the artifact is unreadable")),
            submitted="# gone",
            listed=OSError("the library is unreadable"),
        )
        assert status == 503
        assert not reached

    @pytest.mark.asyncio
    async def test_a_read_that_raised_does_not_reach_the_gate_with_empty_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate is never ASKED about a snapshot nobody read -- an answer of `""`
        from it would be indistinguishable from a legitimate pass."""
        asked: list[Any] = []
        status, reached = await _update_against_a_store(
            monkeypatch,
            reader=_raises(OSError("the artifact is unreadable")),
            submitted=_package_text("crewmate:nyx"),
            gate=lambda *a: asked.append(a) or "",  # type: ignore[func-returns-value]
        )
        assert status == 503
        assert asked == [], "the gate was asked to judge a snapshot that was never read"
        assert not reached

    @pytest.mark.asyncio
    async def test_a_package_write_to_an_empty_slug_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is stored, so there is no binding to authorize against and no token
        to carry: the race where the owner creates this slug mid-write is the one case
        the store's own 404 does not already cover."""
        status, reached = await _update_against_a_store(
            monkeypatch,
            reader=_raises(_not_found()),
            submitted=_package_text("crewmate:nyx"),
        )
        assert status == 409
        assert not reached, "the write raced the creation of the slug it was judged on"

    @pytest.mark.asyncio
    async def test_an_ordinary_write_to_an_empty_slug_still_reaches_the_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An absent slug is the store's own 404 to answer, and it says which slug.
        Only a package write is turned away earlier than that."""
        status, reached = await _update_against_a_store(
            monkeypatch, reader=_raises(_not_found()), submitted="# just a note"
        )
        assert reached, "an ordinary write stopped reaching the store"
        assert status == 0


def _not_found() -> Exception:
    from kiro_crew.artifacts import ArtifactNotFoundError

    return ArtifactNotFoundError("no artifact 'page'")


def _raises(exc: Exception) -> Any:
    def _get(_slug: str, **_kwargs: Any) -> Any:
        # ``**_kwargs`` because the real ``get`` takes ``assign_token``: a fake with a
        # narrower signature would raise TypeError and be read as an unreadable record,
        # which is a different case from the one each test means.
        raise exc

    return _get


async def _update_against_a_store(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reader: Any,
    submitted: str,
    gate: Any = None,
    listed: Any = (),
) -> tuple[int, bool]:
    """``(status, whether the store was written)`` for one ``api_artifact_update``.

    ``status`` is ``0`` when the write reached the store, because the harness stops it
    there rather than building an artifact complete enough to serialize.
    """
    import json
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers import artifacts as handler

    reached: list[bool] = []

    def _update(_slug: str, **_kwargs: Any) -> Any:
        reached.append(True)
        raise _Wrote

    def _list(kind: str | None = None, **_k: Any) -> list[Any]:
        # The LISTING, which is metadata: it answers the kind when the record's own
        # bytes do not. An exception here is the case where nothing can say.
        if isinstance(listed, Exception):
            raise listed
        return [SimpleNamespace(slug=slug, kind="dashboard") for slug in listed]

    monkeypatch.setattr(
        handler,
        "get_default_store",
        lambda: SimpleNamespace(get=reader, update=_update, list=_list),
    )
    monkeypatch.setattr(handler, "_is_restricted_session", lambda *_a: False)

    async def _allowed(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(handler, "_non_owner_refusal", _allowed)
    if gate is not None:
        monkeypatch.setattr(handler, "_package_write_refusal", gate)

    class _Request(JsonBodyRequest):
        headers = {"X-Internal-Secret": "s", "X-Session-Key": "chat-9-17911"}
        match_info = {"slug": "page"}
        app = {"state": SimpleNamespace()}

        def body_bytes(self) -> bytes:
            return json.dumps({"content": submitted}).encode()

        def get(self, key: str, default: Any = None) -> Any:
            return default

    try:
        response = await handler.api_artifact_update(_Request())  # type: ignore[arg-type]
    except _Wrote:
        return 0, True
    return int(response.status), bool(reached)


class _Wrote(Exception):
    """The fake store reached the write. Carried as a raise so the harness measures the
    call rather than a stand-in return value."""


async def _update_through_the_route(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stored: str,
    submitted: str,
    expected_token: str | None = None,
    stored_kind: str = "dashboard",
    submitted_kind: str | None = None,
) -> dict[str, Any]:
    """Drive ``api_artifact_update`` over a store that records the write it is handed.

    The owner gate and the restricted-session check are the artifact route's own and
    have their own tests; what this harness isolates is which token reaches the store.
    """
    import json
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers import artifacts as handler

    captured: dict[str, Any] = {}

    def _update(slug: str, **kwargs: Any) -> Any:
        # STOPS HERE, because what reaches the store IS the measurement: returning a
        # stand-in artifact would mean keeping a second fake true -- the response
        # serializer reads a dozen fields off it -- for a value no assertion looks at.
        captured.update(kwargs)
        raise _Wrote

    store = SimpleNamespace(
        get=lambda slug, **_k: SimpleNamespace(
            slug=slug, content=stored, content_token="token-of-the-read", kind=stored_kind
        ),
        update=_update,
    )
    # PATCHED ON THE HANDLER, which imports the accessor at module scope: patching
    # `kiro_crew.artifacts.get_default_store` leaves this module's own name bound to the
    # real store, and the route answers 404 off the developer's own library.
    monkeypatch.setattr(handler, "get_default_store", lambda: store)
    monkeypatch.setattr(handler, "_is_restricted_session", lambda *_a: False)

    async def _allowed(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(handler, "_non_owner_refusal", _allowed)
    # The gate's own answer is not what this measures: it has its own tests above, and
    # a refusal here would stop the write before the token could be read.
    monkeypatch.setattr(handler, "_package_write_refusal", lambda *_a: "")

    body: dict[str, Any] = {"content": submitted}
    if expected_token is not None:
        body["expected_token"] = expected_token
    if submitted_kind is not None:
        body["kind"] = submitted_kind

    class _Request(JsonBodyRequest):
        headers = {"X-Internal-Secret": "s", "X-Session-Key": "chat-1-1790"}
        match_info = {"slug": "page"}
        app = {"state": SimpleNamespace()}

        def body_bytes(self) -> bytes:
            return json.dumps(body).encode()

        def get(self, key: str, default: Any = None) -> Any:
            return default

    try:
        await handler.api_artifact_update(_Request())  # type: ignore[arg-type]
    except _Wrote:
        return captured
    raise AssertionError("the route never reached the store")


async def _update_body_through_the_route(
    monkeypatch: pytest.MonkeyPatch, *, stored: str, body: dict[str, Any]
) -> tuple[int, dict[str, Any] | None]:
    """``(status, what reached the store)`` for an arbitrary update body.

    :func:`_update_through_the_route`'s sibling for a write that sends NO content, which
    that one cannot express: it always submits a page.
    """
    import json
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers import artifacts as handler

    captured: dict[str, Any] = {}

    def _update(slug: str, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise _Wrote

    store = SimpleNamespace(
        get=lambda slug, **_k: SimpleNamespace(
            slug=slug, content=stored, content_token="token-of-the-read", kind="dashboard"
        ),
        update=_update,
    )
    monkeypatch.setattr(handler, "get_default_store", lambda: store)
    monkeypatch.setattr(handler, "_is_restricted_session", lambda *_a: False)

    async def _allowed(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(handler, "_non_owner_refusal", _allowed)
    monkeypatch.setattr(handler, "_package_write_refusal", lambda *_a: "")

    class _Request(JsonBodyRequest):
        headers = {"X-Internal-Secret": "s", "X-Session-Key": "chat-1-1790"}
        match_info = {"slug": "page"}
        app = {"state": SimpleNamespace()}

        def body_bytes(self) -> bytes:
            return json.dumps(body).encode()

        def get(self, key: str, default: Any = None) -> Any:
            return default

    try:
        response = await handler.api_artifact_update(_Request())  # type: ignore[arg-type]
    except _Wrote:
        return 200, captured
    return response.status, None


class TestAContentLessWriteIsFencedForAPackage:
    """THE WINDOW, CLOSED UNDER THE STORE'S OWN LOCK.

    The update route authorizes a dashboard-package write against the snapshot it READ
    (`handlers/artifacts.py` reads at :2028, gates at :2081, writes at :2111) and hands
    the store that read's ``content_token`` so a rebind landing in the gap becomes a
    conflict rather than a silent overwrite.

    A comparison limited to writes that carry CONTENT leaves a ``kind``-only PATCH
    arriving with the token attached and ignored, so an agent de-authorized by a rebind
    inside that gap can still flip the kind of the package the owner has just taken. No
    bytes are lost and later writes stay owner-gated, but ``dashboard`` is not a
    user-selectable kind, so nothing puts it back without a human -- a page a departing
    agent can break.

    The comparison belongs in the store because only the store holds the lock the
    record is loaded under; the handler's read is by definition a moment earlier. And it
    is scoped by the kind the RECORD holds, never the kind submitted -- the dangerous
    PATCH submits ``markdown``, so reading the submitted value would let exactly this
    write escape the guard it is meant to trip.

    Written against a real store on disk, because what is under test is the comparison
    inside its lock.
    """

    @pytest.fixture()
    def live(self, tmp_path: Any) -> Any:
        from types import SimpleNamespace

        from kiro_crew.artifacts import ArtifactStore

        store = ArtifactStore(root=tmp_path / "artifacts")
        art = store.create(
            name="page",
            kind="dashboard",
            content=_package_text("crewmate:atlas"),
            source="dashboard",
        )
        # The token the route's gate would have read, before any rebind.
        read = store.get(art.slug, assign_token=True)
        return SimpleNamespace(store=store, slug=art.slug, token=read.content_token)

    def test_a_stale_token_refuses_a_content_less_kind_flip(self, live: Any) -> None:
        """The race itself. The owner's rebind lands between the gate's read and this
        write, which rotates the content token -- so the token the gate authorized is
        stale, and the kind flip it was carrying must not apply."""
        from kiro_crew.artifacts import ArtifactConflictError

        # THE REBIND, which is a content write and so rotates the token. Under its own
        # read's token, as every write that lands on a dashboard now must be.
        live.store.update(
            live.slug,
            content=_package_text("crewmate:nyx"),
            actor="user",
            snapshot_token=live.store.get(live.slug, assign_token=True).content_token,
        )
        with pytest.raises(ArtifactConflictError):
            live.store.update(live.slug, kind="markdown", expected_token=live.token)
        # Nothing moved: the record is still a dashboard package.
        assert live.store.get(live.slug).kind == "dashboard"

    def test_the_current_token_lets_the_same_write_through(self, live: Any) -> None:
        """The limit, and why this is a fence rather than a refusal: the operation is
        legitimate when the caller is writing against what the record actually holds."""
        current = live.store.get(live.slug, assign_token=True).content_token
        live.store.update(live.slug, kind="markdown", expected_token=current)
        assert live.store.get(live.slug).kind == "markdown"

    def test_a_rename_with_no_token_is_still_unguarded(self, tmp_path: Any) -> None:
        """THE BEHAVIOUR NOT WIDENED. A metadata-only write on an ordinary artifact
        keeps last-write-wins, which the editor's autosave and every tag edit depend
        on. Guarding those would be a product change hidden inside a bug fix."""
        from kiro_crew.artifacts import ArtifactStore

        store = ArtifactStore(root=tmp_path / "plain")
        art = store.create(name="note", kind="markdown", content="# note", source="dashboard")
        store.update(art.slug, name="Renamed")
        assert store.get(art.slug).name == "Renamed"
        # And a content-less write on an ordinary record with a STALE token is still
        # unguarded, which is the same rule stated where it could have been widened.
        stale = "0" * 64
        store.update(art.slug, name="Renamed twice", expected_token=stale)
        assert store.get(art.slug).name == "Renamed twice"


class TestAnUntokenedWriteCannotTakeADashboardOver:
    """THE SAME WINDOW FROM THE OTHER SIDE: the caller read a record that was not a
    dashboard, so it sent no token at all.

    The class above fences a write that arrives with a STALE token. It cannot fence
    one that arrives with NONE, and the route only attaches a token when the snapshot
    it read -- or the content submitted -- classifies as a package. So a PATCH of
    ``{"kind": "markdown", "content": ...}`` against a slug holding an ordinary
    artifact, or holding nothing, is forwarded with ``expected_token=None``; if the
    owner's dashboard arrives in the gap, the store had nothing to compare and
    rewrote both the kind and the content of a page it was never authorized for.

    So the rule is stated ONCE, in the store, on the record that lock loaded:

        a record whose CURRENT kind is ``dashboard`` is never written without a
        matching snapshot token, and an ABSENT token is a REFUSAL.

    The handler classifies nothing any more. It reports the token of the read it
    authorized against -- ``snapshot_token``, for every write it makes -- and the
    store decides. Three rounds of scoping this in the handler each left the race in
    the case the handler had not classified; a time-of-check guard in the caller
    cannot close a time-of-use hole in the store, and this is the time of use.

    SCOPED to writes that can take the page away: its ``kind``, its bytes, or any
    write whose caller named a snapshot. A rename naming none keeps last-write-wins,
    which is what ``publish_sync``'s snapshot bump and the deploy route's
    ``webapp_metadata`` write depend on -- neither reads the record first and neither
    can move a page to another owner.
    """

    @pytest.fixture()
    def owners_page(self, tmp_path: Any) -> Any:
        from types import SimpleNamespace

        from kiro_crew.artifacts import ArtifactStore

        store = ArtifactStore(root=tmp_path / "artifacts")
        art = store.create(
            name="page",
            kind="dashboard",
            content=_package_text("crewmate:atlas"),
            source="dashboard",
        )
        return SimpleNamespace(store=store, slug=art.slug)

    def test_an_absent_token_cannot_flip_the_kind(self, owners_page: Any) -> None:
        """FAIL CLOSED ON ABSENT. The caller read an ordinary artifact, so nothing
        classified this as a package write and no token travelled at all."""
        from kiro_crew.artifacts import ArtifactConflictError

        with pytest.raises(ArtifactConflictError):
            owners_page.store.update(owners_page.slug, kind="markdown")
        assert owners_page.store.get(owners_page.slug).kind == "dashboard"

    def test_an_absent_token_cannot_replace_the_page_with_prose(self, owners_page: Any) -> None:
        """THE REPORTED PATCH, both fields at once, which is what gets past the
        package validator.

        A content-only write is already refused, but by the layout validator rather
        than by any fence: the record is still a ``dashboard``, so ``# replacement``
        is read as a package and fails to parse. Sending ``kind`` in the same call
        sets the record's kind FIRST, so the validator never runs on the new bytes --
        which is why the two fields together are the shape worth pinning, and why
        this finding was strictly worse than the content-less one before it.
        """
        from kiro_crew.artifacts import ArtifactConflictError

        with pytest.raises(ArtifactConflictError):
            owners_page.store.update(owners_page.slug, kind="markdown", content="# replacement")
        held = owners_page.store.get(owners_page.slug)
        assert held.kind == "dashboard"
        assert "crewmate:atlas" in (held.content or "")

    def test_a_stale_token_is_refused_the_same_way(self, owners_page: Any) -> None:
        """One rule, so the token that moved on and the token that never came are the
        same refusal rather than two guards that could disagree."""
        from kiro_crew.artifacts import ArtifactConflictError

        stale = "0" * 64
        with pytest.raises(ArtifactConflictError):
            owners_page.store.update(owners_page.slug, kind="markdown", snapshot_token=stale)
        assert owners_page.store.get(owners_page.slug).kind == "dashboard"

    def test_a_matching_token_lets_the_write_through(self, owners_page: Any) -> None:
        """The limit, and why this is a fence rather than a refusal: the owner's own
        write names the record it is landing on, so it is not refused by it."""
        current = owners_page.store.get(owners_page.slug, assign_token=True).content_token
        owners_page.store.update(owners_page.slug, kind="markdown", snapshot_token=current)
        assert owners_page.store.get(owners_page.slug).kind == "markdown"

    def test_the_callers_own_expected_token_also_satisfies_it(self, owners_page: Any) -> None:
        """A caller re-basing on a read of its own is naming the same thing, so its
        ``expected_token`` counts: requiring the handler's spelling instead would
        refuse a correct write for using the documented field."""
        current = owners_page.store.get(owners_page.slug, assign_token=True).content_token
        owners_page.store.update(owners_page.slug, kind="markdown", expected_token=current)
        assert owners_page.store.get(owners_page.slug).kind == "markdown"

    def test_a_metadata_only_write_naming_no_snapshot_still_applies(self, owners_page: Any) -> None:
        """THE BEHAVIOUR NOT WIDENED, and the one case this scoping exists for.

        ``publish_sync`` calls ``update(slug, snapshot=True)`` and the deploy route
        calls it with ``webapp_metadata`` only. Neither reads the record first, so
        neither can name a snapshot -- and neither can take a page from its owner.
        Requiring a token of them would have broken both to close a hole in neither.
        """
        owners_page.store.update(owners_page.slug, name="Renamed", tags=["pinned"])
        owners_page.store.update(owners_page.slug, snapshot=True, actor="user")
        held = owners_page.store.get(owners_page.slug)
        assert held.name == "Renamed" and held.kind == "dashboard"

    def test_a_metadata_only_write_that_DOES_name_one_is_fenced(self, owners_page: Any) -> None:
        """And the finding before this one stays closed: a caller that named a
        snapshot is held to it even where the write carries no content, because the
        `kind`-only PATCH that arrived with a stale token was exactly that write."""
        from kiro_crew.artifacts import ArtifactConflictError

        with pytest.raises(ArtifactConflictError):
            owners_page.store.update(owners_page.slug, name="Renamed", snapshot_token="0" * 64)
        assert owners_page.store.get(owners_page.slug).name == "page"

    def test_the_fence_does_not_swallow_the_callers_own_stale_token(self, owners_page: Any) -> None:
        """TWO QUESTIONS, ASKED SEPARATELY.

        The fence asks whether this write names the record it is landing on, and the
        handler can always answer it with its own fresh read. The caller's
        ``expected_token`` asks something else: whether the record still holds what
        THAT client last saw. Answering the first with the handler's token and then
        skipping the second lets an owner's stale tab overwrite a newer layout with
        no 409 -- an agent and an open tab editing one dashboard is the ordinary case
        for this feature, not an extreme one.
        """
        from kiro_crew.artifacts import ArtifactConflictError

        stale = owners_page.store.get(owners_page.slug, assign_token=True).content_token
        # The newer layout lands, rotating the token the tab is holding.
        fresh = owners_page.store.update(
            owners_page.slug,
            content=_package_text("crewmate:nyx"),
            snapshot_token=stale,
        )
        assert fresh is not None
        current = owners_page.store.get(owners_page.slug, assign_token=True).content_token
        assert current != stale, "the rebind did not rotate the token; this proves nothing"
        with pytest.raises(ArtifactConflictError):
            owners_page.store.update(
                owners_page.slug,
                content=_package_text("crewmate:atlas"),
                snapshot_token=current,
                expected_token=stale,
            )
        assert "nyx" in (
            owners_page.store.get(owners_page.slug).content or ""
        ), "the newer layout was overwritten by a write the caller based on an older read"

    def test_both_tokens_current_still_goes_through(self, owners_page: Any) -> None:
        """The limit of the pair: a caller re-basing on the current record is asking
        for exactly this write, and neither question refuses it."""
        current = owners_page.store.get(owners_page.slug, assign_token=True).content_token
        owners_page.store.update(
            owners_page.slug,
            content=_package_text("crewmate:nyx"),
            snapshot_token=current,
            expected_token=current,
        )
        assert "nyx" in (owners_page.store.get(owners_page.slug).content or "")

    def test_an_ordinary_artifact_keeps_last_write_wins(self, tmp_path: Any) -> None:
        """The fence is one kind's, not the store's. A prose document still takes a
        content write with no token, which the editor's autosave depends on."""
        from kiro_crew.artifacts import ArtifactStore

        store = ArtifactStore(root=tmp_path / "plain")
        art = store.create(name="note", kind="markdown", content="# note", source="dashboard")
        store.update(art.slug, content="# rewritten")
        assert "rewritten" in (store.get(art.slug).content or "")

    @pytest.mark.asyncio
    async def test_the_update_route_tells_the_store_what_it_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AND THE ROUTE IS WHAT MUST SAY IT, on the write that carries no token.

        This is the reported PATCH as the route sees it: an ordinary artifact at the
        read, ``markdown`` and prose submitted. Nothing classifies it as a package, so
        no token is attached -- and what reaches the store instead is the kind the read
        found, which is the only thing that lets the store refuse once the record has
        become somebody's dashboard.
        """
        wrote = await _update_through_the_route(
            monkeypatch,
            stored="# yesterday's note",
            stored_kind="markdown",
            submitted="# replacement",
            submitted_kind="markdown",
        )
        assert wrote["expected_token"] is None, "the caller sent none, which is the shape"
        assert wrote["snapshot_token"] == "token-of-the-read", (
            "the route wrote without naming the read it authorized against, so the "
            "store cannot tell this write from the owner's own"
        )

    @pytest.mark.asyncio
    async def test_the_route_names_its_read_on_a_prose_write_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The handler classifies NOTHING: the same token travels whatever the slug
        held, which is what stops the next unclassified case being the next hole."""
        wrote = await _update_through_the_route(
            monkeypatch,
            stored="# yesterday's note",
            stored_kind="markdown",
            submitted="# today's note",
        )
        assert wrote["snapshot_token"] == "token-of-the-read"

    @pytest.mark.asyncio
    async def test_the_store_refuses_exactly_what_the_route_hands_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """THE TWO HALVES JOINED, so neither can be true alone.

        The route's own declaration is fed to a REAL store holding a dashboard, which
        is the interleaving the attack chooses: read an ordinary artifact, have the
        owner's page land on the slug, then write.
        """
        from kiro_crew.artifacts import ArtifactConflictError, ArtifactStore

        wrote = await _update_through_the_route(
            monkeypatch,
            stored="# yesterday's note",
            stored_kind="markdown",
            submitted="# replacement",
            submitted_kind="markdown",
        )
        store = ArtifactStore(root=tmp_path / "artifacts")
        page = store.create(
            name="page",
            kind="dashboard",
            content=_package_text("crewmate:atlas"),
            source="dashboard",
        )
        # The harness's token is a readable stand-in rather than a real digest, so it
        # is respelled as a syntactically valid one the store will actually compare.
        # What carries over from the route is that a token travelled AT ALL and that it
        # describes the record the caller read, not this page.
        assert wrote.get("snapshot_token"), "the route named no read, so this proves nothing"
        with pytest.raises(ArtifactConflictError):
            store.update(
                page.slug,
                kind=wrote.get("kind"),
                content=wrote.get("content"),
                expected_token=wrote.get("expected_token"),
                snapshot_token="0" * 64,
            )
        assert store.get(page.slug).kind == "dashboard"


class _AgentRequest:
    """An MCP caller, with the crew it writes as resolved for it.

    ``package_write_refusal`` reads the caller through ``writing_caller``, which goes to
    the session's own agent binding; these tests are about the STORED side of the gate,
    so the caller is pinned rather than staged through a roster.
    """

    def __init__(self) -> None:
        self.headers = {"X-Internal-Secret": "s", "X-Session-Key": "chat-9-17911"}

    def get(self, key: str, default: Any = None) -> Any:
        return default


@pytest.fixture(autouse=True)
def _pin_the_caller(_floor_monkeypatch: Any) -> None:
    """Bind ``_agent_request``'s crew for the two classes below.

    ``_floor_monkeypatch``, not the test-owned ``monkeypatch``: an autouse fixture
    patching through the shared one is lifted by any test that calls
    ``monkeypatch.undo()``, which would point the gate back at the real caller
    resolution mid-test and authorize a write this suite means to refuse.
    """
    from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

    _floor_monkeypatch.setattr(mdf, "writing_caller", lambda req: (getattr(req, "_crew", ""), ""))


def _agent_request(*, crew: str) -> Any:
    req = _AgentRequest()
    req._crew = crew  # type: ignore[attr-defined]
    return req


class TestOwnershipDoesNotDependOnLayoutValidity:
    """A STORED PACKAGE THE VALIDATOR REJECTS STILL HAS AN OWNER.

    The write gate authorizes against BOTH bindings, and the stored one decides whether
    the slug may be written at all -- checking only the submitted binding authorizes the
    caller against a claim the caller wrote.

    The stored binding was read by PARSING the stored package, so a package the current
    validator rejects classified as "not a package": the gate then skipped it and
    checked only the incoming binding, and a worker submitting a package bound to itself
    could overwrite and rebind a crewmate's page.

    This PR is what makes that reachable. It adds the credential-shaped field-name
    refusal, so a dashboard stored BEFORE it -- whose model the old validator accepted --
    now fails `parse_package`. An ordinary existing record is enough.

    So ownership is read off the stored ENVELOPE, independently of whether the layout
    still validates, and a stored record whose ownership cannot be established at all is
    REFUSED rather than skipped. Same reasoning as `resolve_bound_slug` matching the
    envelope rather than a validated package: a layout the validator rejects is still
    somebody's.
    """

    #: A package the CURRENT validator rejects, for the reason this PR introduced: a
    #: field name shaped like a credential. Its `bound_to` is perfectly readable.
    STALE_BUT_OWNED = (
        '{"kind": "dashboard", "bound_to": "crewmate:atlas", '
        '"model": {"types": {"ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": '
        '{"type": "text", "label": "L", "description": "d", '
        '"source": {"fold": "work", "path": "goal"}}}}, '
        '"view": {"html": "<p>x</p>"}, "theme": {"tokens": {}}}'
    )

    def test_the_stored_package_really_fails_the_current_validator(self) -> None:
        """The premise. Without it this class could pass by measuring a package that
        validates fine, which is the case that was never broken."""
        from kiro_crew.artifact_store import dashboard_package as dp

        with pytest.raises(dp.ArtifactValidationError):
            dp.parse_package(self.STALE_BUT_OWNED)

    def test_its_binding_is_still_enforced_against_another_crewmate(self) -> None:
        """THE BYPASS. A worker submits a package bound to ITSELF over a stored one
        bound to Atlas. The stored binding is the one that decides, and it is readable
        whatever the layout does."""
        from kiro_crew.dashboard.handlers import member_dashboard_fields as fields

        refused = fields.package_write_refusal(
            _agent_request(crew="nyx"),
            _package_text("crewmate:nyx"),
            self.STALE_BUT_OWNED,
        )
        assert refused, (
            "a stored package the validator rejects let another crewmate's write "
            "through, so only the caller's own claim was checked"
        )
        assert "atlas" in refused.lower() or "bound" in refused.lower()

    def test_the_owner_itself_is_still_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The limit: reading ownership off the envelope must not lock the real owner
        out of repairing the very package the validator now rejects."""
        # A roster whose keys ARE the names these callers use, because the gate now
        # requires the binding's slug to name exactly one crewmate in it.
        _install_roster(monkeypatch, {"atlas": "atlas", "nyx": "nyx"})
        from kiro_crew.dashboard.handlers import member_dashboard_fields as fields

        assert (
            fields.package_write_refusal(
                _agent_request(crew="atlas"),
                _package_text("crewmate:atlas"),
                self.STALE_BUT_OWNED,
            )
            == ""
        )

    def test_a_stored_package_with_no_readable_binding_is_refused(self) -> None:
        """Ownership that cannot be ESTABLISHED is not ownership that is absent. A
        record naming the dashboard kind with no usable `bound_to` is refused rather
        than skipped, which is the fail-closed direction."""
        from kiro_crew.dashboard.handlers import member_dashboard_fields as fields

        refused = fields.package_write_refusal(
            _agent_request(crew="nyx"),
            _package_text("crewmate:nyx"),
            '{"kind": "dashboard", "bound_to": 17, "model": {}, "view": {}, "theme": {}}',
        )
        assert refused, "a dashboard record with an unreadable binding was skipped"

    def test_an_ordinary_document_is_still_not_a_package(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other limit: prose stored at the slug carries no binding to enforce, so
        the gate must not start refusing writes over every markdown note."""
        _install_roster(monkeypatch, {"atlas": "atlas", "nyx": "nyx"})
        from kiro_crew.dashboard.handlers import member_dashboard_fields as fields

        assert (
            fields.package_write_refusal(
                _agent_request(crew="nyx"), _package_text("crewmate:nyx"), "# just a note"
            )
            == ""
        )


class TestTheOwnerRefusalRedactsEveryNameItQuotes:
    """AND THE THIRD PATH, WHICH IS WHERE THE RULE NOW LIVES.

    The declared-name check and the `view.html` validator each learned to redact at
    their own call site. ``binding_refusal`` is a third reader of the same string and
    did not: ``_BOUND_TO_RE`` accepts a name shaped like a GitLab PAT or a Slack bot
    token, the refusal quotes that name, and the named dashboard GET returns it as the
    HTTP 403 body.

    So the redaction moved INTO ``binding_refusal``, the one function that composes
    these sentences, rather than onto the route that returns them. Three call sites had
    the same defect and two of them were found one at a time; the fourth caller to
    read this string gets the rule for free.
    """

    @staticmethod
    def _tokens() -> list[str]:
        """Shapes the redactor rewrites that ``_BOUND_TO_RE`` also accepts.

        No ``_`` and no upper case, which rules out a ``ghp_`` or an AWS key for a
        BINDING and leaves these two.

        ASSEMBLED rather than written out: these are synthetic shapes, but GitHub's
        push protection matches the shape and refuses the push, and asking for a
        scanning exemption to land a test fixture would spend a real exemption on a
        string that is not a secret. The value the redactor sees is the same.
        """
        return ["gl" + "pat-" + "a" * 20, "xo" + "xb-" + "1" * 12 + "-" + "abcdefghijklmnop"]

    def test_a_crewmate_binding_is_not_echoed(self) -> None:
        from kiro_crew.artifact_store import dashboard_package as dp

        for secret in self._tokens():
            refusal = dp.binding_refusal(f"crewmate:{secret}", crew="Atlas")
            assert refusal, "a mismatched binding was not refused at all"
            assert secret not in refusal, (
                f"the owner refusal echoed {secret[:6]}... back to its sender, and the "
                "named dashboard GET returns this string as its HTTP body"
            )

    def test_a_session_binding_is_not_echoed_either(self) -> None:
        """The branch GPT named beside the crewmate one. Both quote a name."""
        from kiro_crew.artifact_store import dashboard_package as dp

        for secret in self._tokens():
            refusal = dp.binding_refusal(f"session:{secret}", slot="chat-1-1790")
            assert refusal and secret not in refusal

    def test_the_callers_own_name_is_not_echoed(self) -> None:
        """The refusal quotes the CALLER too, and a crew name reaches it from the
        session's binding rather than from a body -- but it is still a name in a
        sentence that leaves the process."""
        from kiro_crew.artifact_store import dashboard_package as dp

        secret = "gl" + "pat-" + "b" * 20  # assembled; see _tokens for why
        refusal = dp.binding_refusal("crewmate:atlas", crew=secret)
        assert refusal and secret not in refusal

    def test_a_validation_error_branch_is_redacted(self) -> None:
        """The third branch: a binding that fails validation for a reason unrelated to
        its shape -- a missing scope -- still has its value quoted in the error."""
        from kiro_crew.artifact_store import dashboard_package as dp

        secret = "gl" + "pat-" + "c" * 20  # assembled; see _tokens for why
        refusal = dp.binding_refusal(secret)
        assert refusal and secret not in refusal

    def test_an_ordinary_name_is_still_said_plainly(self, roster: None) -> None:
        """The limit. Redaction is identity on a name that is not credential-shaped, so
        the refusal keeps naming whose dashboard it is, which is what makes it useful
        to the worker agent that reads it."""
        from kiro_crew.artifact_store import dashboard_package as dp

        refusal = dp.binding_refusal("crewmate:nyx", crew="Atlas")
        assert "nyx" in refusal and "Atlas" in refusal
        assert dp.binding_refusal("crewmate:atlas", crew="Atlas") == ""


class TestAnUndeclaredBindingNameIsRedactedInTheRefusal:
    """THE SAME RULE, ON THE SIBLING PATH THAT MISSED IT.

    A field NAME travels further than its value, so a name shaped like a credential is
    refused as a name -- and the refusal names it by its REDACTED spelling, because
    echoing back the thing just identified as credential-shaped puts it exactly where
    the check exists to keep it out of.

    That redaction ran only on DECLARED `model.types` names. The `view.html` validator
    lists the UNDECLARED `data-dashboard-field` names it found straight into its
    refusal, and the artifact route returns that string as the HTTP error body -- so an
    agent submitting a page that binds a credential-shaped name it never declared gets
    the credential echoed back unredacted.
    """

    def test_an_undeclared_credential_shaped_binding_is_not_echoed(self) -> None:
        from kiro_crew.artifact_store import dashboard_package as dp

        secret = "ghp_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        package = {
            "kind": "dashboard",
            "bound_to": "crewmate:atlas",
            "model": {
                "types": {
                    "goal": {
                        "type": "text",
                        "label": "Goal",
                        "description": "d",
                        "source": {"fold": "work", "path": "goal"},
                    }
                }
            },
            "view": {"html": f'<p data-dashboard-field="{secret}"></p>'},
            "theme": {"tokens": {}},
        }
        with pytest.raises(dp.ArtifactValidationError) as refused:
            dp.validate_package(package)
        message = str(refused.value)
        assert secret not in message, (
            "the refusal echoed an undeclared credential-shaped binding name back to "
            "its sender, which is the hazard the declared-name check exists for"
        )
        # Still usable: it has to say WHICH name, or the author cannot fix the page.
        assert "does not declare" in message

    def test_an_ordinary_undeclared_binding_is_still_named_plainly(self) -> None:
        """The limit. Redaction is identity on a name that is not credential-shaped, so
        the ordinary refusal keeps naming the field the author has to fix."""
        from kiro_crew.artifact_store import dashboard_package as dp

        package = {
            "kind": "dashboard",
            "bound_to": "crewmate:atlas",
            "model": {
                "types": {
                    "goal": {
                        "type": "text",
                        "label": "Goal",
                        "description": "d",
                        "source": {"fold": "work", "path": "goal"},
                    }
                }
            },
            "view": {"html": '<p data-dashboard-field="typo_field"></p>'},
            "theme": {"tokens": {}},
        }
        with pytest.raises(dp.ArtifactValidationError) as refused:
            dp.validate_package(package)
        assert "typo_field" in str(refused.value)


class TestTheFenceAndTheGateClassifyTheSameWay:
    """OWNERSHIP AND THE FENCE ARE HALVES OF ONE ANSWER.

    The route does two things with the stored snapshot: it decides WHO owns the record
    (so the write can be refused) and it decides whether to forward that read's
    ``content_token`` (so a rebind landing in the gap becomes a conflict). Reading the
    record one way for the first and another way for the second is worse than either
    reading on its own.

    That is what a layout-invalid stored package produced. Ownership moved to the
    envelope, so a legacy package whose field name the current validator rejects is
    enforced -- and the token decision still PARSED, so the same record classified as
    "not a package" and no token was forwarded. Its own former owner's kind-only PATCH
    then passed the gate with nothing fencing it, and a concurrent repair or rebind was
    committed over.

    So both read the record the same way.
    """

    def test_a_layout_invalid_package_still_forwards_the_snapshot_token(self) -> None:
        from kiro_crew.dashboard.handlers import artifacts as handler

        # The same record the ownership tests use: readable binding, model the current
        # validator rejects.
        stale_but_owned = TestOwnershipDoesNotDependOnLayoutValidity.STALE_BUT_OWNED
        assert handler._is_dashboard_package(stale_but_owned, None), (
            "the token decision classified a layout-invalid package as 'not a package', "
            "so the route would authorize the write against its envelope and then "
            "forward no token to fence it"
        )

    def test_the_two_classifiers_agree_on_every_shape_that_matters(self) -> None:
        """Stated as the agreement rather than as one case, because the defect was the
        disagreement. Anything the gate will enforce a binding for must also be fenced.
        """
        from kiro_crew.dashboard.handlers import artifacts as handler
        from kiro_crew.dashboard.handlers import member_dashboard_fields as fields

        shapes = [
            _package_text("crewmate:atlas"),
            TestOwnershipDoesNotDependOnLayoutValidity.STALE_BUT_OWNED,
            '{"kind": "dashboard", "bound_to": 17, "model": {}, "view": {}, "theme": {}}',
        ]
        for text in shapes:
            gate_cares = fields.stored_package_binding(text) is not None
            fenced = handler._is_dashboard_package(text, None)
            assert gate_cares == fenced, (
                f"the gate and the fence disagree about {text[:48]!r}: the gate "
                f"{'enforces' if gate_cares else 'ignores'} it and the fence "
                f"{'guards' if fenced else 'does not guard'} it"
            )

    def test_prose_is_still_neither(self) -> None:
        """The limit: an ordinary document is not a package to either of them, so a
        markdown note keeps last-write-wins."""
        from kiro_crew.dashboard.handlers import artifacts as handler
        from kiro_crew.dashboard.handlers import member_dashboard_fields as fields

        assert fields.stored_package_binding("# just a note") is None
        assert not handler._is_dashboard_package("# just a note", None)

    def test_a_document_too_deep_to_decode_is_not_a_package_and_does_not_raise(
        self,
    ) -> None:
        """AND IT MUST NOT LOCK ITS OWNER OUT.

        A markdown document can begin with ``{`` and nest deeper than the JSON
        decoder's stack. ``RecursionError`` is a ``RuntimeError``, so the
        ``(ValueError, TypeError)`` arm never saw it: it escaped, and every later
        PATCH of that slug answered 500 -- including the one replacing the content
        with plain text, so nothing could undo it.

        ``None`` rather than ``BINDING_UNPROVABLE`` is the answer, and the write path
        is why: ``dashboard_package._decode_package`` refuses exactly this depth, so
        no stored dashboard record can be this deep. Calling it unprovable would
        refuse every write to an ordinary document instead.
        """
        from kiro_crew.dashboard.handlers import artifacts as handler
        from kiro_crew.dashboard.handlers import member_dashboard_fields as fields

        too_deep = '{"items": ' + "[" * 100_000 + "]" * 100_000 + "}"
        assert fields.stored_package_binding(too_deep) is None
        assert not handler._is_dashboard_package(too_deep, None)
        # And the gate built on it answers rather than raising, which is what the 500
        # was: the refusal string is empty because there is no stored owner to protect.
        assert fields.package_write_refusal(None, "# plain text now", too_deep) == ""


class TestAValidationRefusalDoesNotEchoACredential:
    """THE VALIDATOR QUOTES THE VALUE IT REFUSED, and the render path returns that
    sentence as an HTTP 409 body.

    A refusal names the offending key and its value so the author can fix the page --
    and a value in a stored package can be credential-shaped, because a snapshot on
    disk is not re-validated by the writer that put it there. So the quoted text is
    redacted before it leaves the process, the same rule the declared-name check, the
    ``view.html`` validator and ``binding_refusal`` already follow.

    Pinned at the route, because the error itself is the validator's to word: what this
    change owns is that the route does not hand it to a reader unredacted.
    """

    def test_the_route_redacts_the_validator_message(self) -> None:
        import inspect

        from kiro_crew.dashboard.handlers import member_dashboard as md

        src = inspect.getsource(md._serve_package)
        flat = " ".join(src.split())
        assert "dashboard_refused" in flat
        assert "redact_via_context(str(exc))" in flat, (
            "the validator's message reaches the 409 body unredacted, so a "
            "credential-shaped value in a stored package is echoed to its reader"
        )

    def test_the_redactor_rewrites_the_shape_the_validator_would_quote(self) -> None:
        """And the redactor really does rewrite such a value, so the call above is not
        a no-op dressed as a guard."""
        from kiro_crew.platform.context import redact_via_context

        secret = "gl" + "pat-" + "e" * 20
        said = f"model.types.goal.type {secret!r} is not a known data type"
        assert secret not in redact_via_context(said)
