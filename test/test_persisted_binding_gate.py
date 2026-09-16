"""Adoption gate for a persisted slot binding.

A persisted binding is adopted only when its candidate key resolves to the same
transcript as the live one, and every adoption is audited.
"""

import pytest


def test_the_gate_refuses_a_legacy_alias_when_both_transcripts_exist(tmp_path):
    """The legacy alias is only one session's second name while only one file is backed.

    Resuming the bare transcript adopts the canonical ``slack:<ts>`` binding, and every later turn
    and save then routes through ``_path("slack:<ts>")``. With one file that resolves back to the
    bare transcript and nothing moves. With both files it resolves to the CANONICAL one, so the bare
    session's turns land in a different live conversation.

    Both arms asserted: refusing when they coexist is the fix, and still ACCEPTING the single-file
    case is what keeps the legacy thread bound at all -- a gate that refused both would pass the
    first assertion while reintroducing the unbound-slot loss the branch exists to prevent.
    """
    from kiro_crew.dashboard.chat_utils import persisted_binding_is_adoptable

    ts = "1700000000.000100"
    canonical_key = f"slack:{ts}"
    turn = '{"role": "user", "content": "a turn"}\n'

    (tmp_path / f"{ts}.jsonl").write_text(turn, encoding="utf-8")
    assert persisted_binding_is_adoptable(canonical_key, ts, sessions_dir=tmp_path), (
        "the lone legacy transcript must still adopt its canonical binding, or the slot comes back "
        "unbound and its authorized context is dropped as foreign"
    )

    (tmp_path / f"slack_{ts}.jsonl").write_text(turn, encoding="utf-8")
    assert not persisted_binding_is_adoptable(canonical_key, ts, sessions_dir=tmp_path), (
        f"the alias was adopted while both {ts}.jsonl and slack_{ts}.jsonl exist: those are two "
        "live sessions, so this slot's turns and saves would land in the canonical conversation"
    )


def test_an_underscored_discord_dm_key_is_measured_against_the_binding_gate(tmp_path):
    """MEASURES the refused class rather than describing it as rare.

    `slot_transcript_key` states that a channel-born slot's name IS its transcript's filename stem,
    and that the live spelling "cannot be recovered this way" because `_safe_key` folds every `:`
    to `_`. So hydration presents the FOLDED stem, and the gate is asked to prove a candidate
    against an ambiguous name. For a slug carrying its own `_`, that proof is unavailable: the
    genuine key and an impostor that substituted `_` for a separator are byte-identical here.

    What this pins is the SIZE of that: which real shapes fall inside the refused class, and that
    the failure direction is the declared safe one -- an unbound slot parks its queue rather than
    dropping it, so nothing acknowledged is lost while the binding is unprovable.
    """
    from kiro_crew.dashboard.chat_utils import persisted_binding_is_adoptable
    from kiro_crew.history import transcript_stem

    # Shapes `messaging.link` can produce: `session_key(channel_type, conversation_id)` takes the
    # conversation id from the provider, so any provider id carrying `_` lands here.
    underscored = [
        "discord:crew_agent:direct:user_1",
        "webex:room_abc:thread_def",
        "wecom:agent_1:user_2",
    ]
    clean = [
        "discord:123456789:direct:987654321",
        "slack:C123:1785370133.085469",
        "telegram:-1001234567890:42",
    ]

    # CONTROL FIRST: a key with no literal underscore must be adoptable from its own stem, so a
    # refusal below is the underscore rule and not a broken fixture.
    for key in clean:
        assert persisted_binding_is_adoptable(
            key, transcript_stem(key)
        ), f"control failed: {key!r} is refused from its own stem, so this measures nothing"

    refused = [k for k in underscored if not persisted_binding_is_adoptable(k, transcript_stem(k))]
    assert refused == underscored, (
        "the refused class is not what this test measures; adoptable now: "
        f"{[k for k in underscored if k not in refused]}"
    )

    # The ambiguity is REAL, not a conservative guess: an impostor that substituted `_` for a
    # separator folds onto the same stem as the genuine key, so the stem cannot tell them apart.
    genuine = "discord:crew_agent:direct:user_1"
    impostor = "discord:crew:agent:direct:user_1"
    assert transcript_stem(genuine) == transcript_stem(impostor), (
        "the two spellings no longer collide, so the refusal has a cheaper discriminator "
        "than this test assumes"
    )


@pytest.mark.parametrize(
    "persisted, transcript_key, why",
    [
        (
            "slack:1700000000.123456",
            "1700000000.123456",
            "pre-migration Slack thread: the transcript is the BARE thread_ts, the persisted "
            "binding is the canonical key",
        ),
        (
            "discord:123:456",
            "discord_123_456",
            "folded stem handed to the binder by list_sessions, which keys on path.stem",
        ),
        (
            "slack:C123:1700000000.1",
            "slack_C123_1700000000.1",
            "channel-scoped Slack key against its own folded stem",
        ),
    ],
)
def test_prior_release_metadata_spellings_pass_the_stem_rule(persisted, transcript_key, why):
    """The trust gate judges spellings written by OLDER releases against TODAY's stem rule.

    `ConversationLog._path` derives a filename two ways and `transcript_stems` mirrors those two,
    so the accepted set is only as correct as the agreement between them. Two spellings have
    already been refused in error from exactly that drift -- the legacy Slack bare `thread_ts` and
    a folded Discord key -- and each cost a slot its binding, dropped its authorized context as
    foreign, then cleared the durable copy on the next save. This is the regression guard for that
    measured class: every row is a spelling a released build could have persisted, so a narrowing
    of the naming rule fails HERE rather than one channel at a time in production.
    """
    from kiro_crew.dashboard.chat_utils import persisted_binding_is_adoptable
    from kiro_crew.history import transcript_stems

    assert persisted_binding_is_adoptable(persisted, transcript_key), (
        f"{why}: a spelling an older release persisted is now refused, so hydration leaves the "
        f"slot unbound and its queued context is dropped as foreign; stems were "
        f"{transcript_stems(persisted)} vs {transcript_stems(transcript_key)}"
    )


def test_a_legacy_bare_slack_transcript_still_adopts_its_canonical_binding():
    """GPT finding C: a legacy bare Slack transcript must keep its binding.

    `ConversationLog._path` falls back to the pre-migration bare ``thread_ts``
    filename, so resuming from THAT file presents `transcript_key` as the bare stem
    while the persisted binding is the canonical ``slack:<ts>``. Neither is the other's
    fold, so the binding was refused, the slot came back unbound, its context was
    dropped as foreign, and the next save cleared the durable copy.
    """
    from kiro_crew.dashboard.chat_utils import persisted_binding_is_adoptable

    canonical = "slack:1785370133.085469"
    legacy_stem = "1785370133.085469"
    assert persisted_binding_is_adoptable(canonical, legacy_stem), (
        "a legacy bare Slack transcript refuses its own canonical binding, so the slot "
        "resumes unbound and loses the context it was holding"
    )
    # The canonical spelling must still work, and an unrelated key must still be refused.
    assert persisted_binding_is_adoptable(canonical, "slack_1785370133.085469")
    assert not persisted_binding_is_adoptable(
        canonical, "slack_9999999999.000000"
    ), "the alias set must not admit an unrelated transcript"


def test_a_fully_folded_multi_segment_channel_key_is_adoptable():
    """A Discord/Slack DM key has MORE than one separator, all folded in the stem.

    An alias that folded only the namespace separator refused
    `discord:DM:12345` <-> `discord_DM_12345`, so a pruned session map dropped the
    binding -- and with it the pending context the slot was holding. The rule is
    "one side IS the other's fold", which covers every segment count.
    """
    from kiro_crew.dashboard.chat_utils import persisted_binding_is_adoptable

    for live, stem in (
        ("discord:DM:12345", "discord_DM_12345"),
        ("slack:C123:1785370133.085469", "slack_C123_1785370133.085469"),
        ("slack:1785370133.085469", "slack_1785370133.085469"),
    ):
        assert persisted_binding_is_adoptable(live, stem), f"{live} <-> {stem} was refused"
        # The REVERSE is refused on purpose: a candidate that is merely the
        # transcript key's fold can be a distinct alias sharing that file.
        assert not persisted_binding_is_adoptable(stem, live), f"{stem} -> {live} was adopted"


def test_the_gate_refuses_two_distinct_keys_that_share_a_folded_stem():
    """The fold is many-to-one, so comparing folded stems adopts a FOREIGN key.

    Measured collision: `slack:C123:1785370133.085469` and
    `slack:C123_1785370133.085469` are distinct sessions whose `_safe_key` stems are
    both `slack_C123_1785370133.085469`, because `_safe_key` substitutes EVERY
    non-[\\w\\-.] character. So the alias set must be enumerated -- identity plus the
    namespace separator only -- not derived from that fold.
    """
    from kiro_crew.dashboard.chat_utils import persisted_binding_is_adoptable
    from kiro_crew.history import transcript_stem

    a = "slack:C123:1785370133.085469"
    b = "slack:C123_1785370133.085469"
    # Precondition: these two really do collide under the fold, so the test is
    # exercising the defect rather than an imagined one.
    assert transcript_stem(a) == transcript_stem(b), "precondition: the stems collide"
    assert a != b
    assert not persisted_binding_is_adoptable(a, b), (
        "a foreign session key was adopted because its FOLDED stem matched -- "
        "subsequent turns would route through another session"
    )
    assert not persisted_binding_is_adoptable(b, a)


def test_the_gate_still_adopts_the_one_documented_alias():
    """The legitimate FORWARD fold must still work; the reverse one must not.

    A gate that simply switched to `==` would pass the collision test above and break
    every genuine binding stored in the filename spelling, so the forward direction is
    pinned here. The REVERSE direction is refused deliberately: accepting a candidate
    that is merely the transcript key's fold adopts a distinct session alias sharing
    one transcript file. That refusal became affordable once an unprovable binding
    stopped destroying the queued copy -- the entries are held and written back
    instead, so strictness does not cost acknowledged content.
    """
    from kiro_crew.dashboard.chat_utils import persisted_binding_is_adoptable

    assert persisted_binding_is_adoptable("slack:1785370133.085469", "slack_1785370133.085469")
    assert persisted_binding_is_adoptable("cron:job-7", "cron:job-7")
    # The reverse fold is REFUSED -- a folded candidate against a canonical key.
    assert not persisted_binding_is_adoptable(
        "slack_1785370133.085469", "slack:1785370133.085469"
    ), "the reverse fold adopts a distinct alias sharing one transcript file"
    # And still refuses genuinely different sessions.
    assert not persisted_binding_is_adoptable("cron:job-7", "cron:job-8")
    assert not persisted_binding_is_adoptable("cron:job-7", "dashboard:chat-1")


def test_the_gate_folds_the_two_spellings_of_one_conversation():
    """One conversation has more than one spelling, so a raw compare is wrong.

    `history._safe_key` folds `slack:<ts>` and the `slack_<ts>` filename stem onto
    the same `.jsonl`, so a legitimate binding written in the other spelling must
    still be adopted -- while a genuinely different session is still refused.
    """
    from kiro_crew.dashboard.chat_utils import persisted_binding_is_adoptable

    assert persisted_binding_is_adoptable("slack:1785370133.085469", "slack_1785370133.085469")
    assert persisted_binding_is_adoptable("cron:job-7", "cron:job-7")
    assert not persisted_binding_is_adoptable("cron:job-7", "cron:job-8")
    assert not persisted_binding_is_adoptable("cron:job-7", "dashboard:chat-1")
    # An empty candidate or transcript is never adoptable.
    assert not persisted_binding_is_adoptable("", "cron:job-7")
    assert not persisted_binding_is_adoptable("cron:job-7", "")


def test_persisted_binding_audit_records_both_outcomes():
    """Both the permit and the refusal reach the SEL, with the permit/deny vocabulary.

    GPT's finding was that the trust gate decided cross-session routing with no
    audit event. Recording only refusals would still leave the ADOPTION -- the
    decision that actually retargets a slot -- untraceable, so both are pinned.
    """
    from unittest.mock import MagicMock, patch

    from kiro_crew.dashboard import chat_utils as cu

    fake = MagicMock()
    with patch.object(cu, "sel", return_value=fake):
        cu.audit_persisted_binding("slack_123", "slack:123", adopted=True)
        cu.audit_persisted_binding("slack_123", "slack:999", adopted=False)

    assert fake.log_governance_decision.call_count == 2
    outcomes = [c.kwargs["outcome"] for c in fake.log_governance_decision.call_args_list]
    assert outcomes == ["allowed", "denied"], outcomes
    first = fake.log_governance_decision.call_args_list[0].kwargs
    assert first["rule"] == "persisted_binding_is_adoptable"
    assert first["item"] == "slack:123"
    assert first["scope"] == "chat.linked_session_key"


def test_persisted_binding_audit_survives_an_unwritable_sel():
    """A SEL write failure must be CONTAINED, not raised out of hydration.

    Audit-or-deny: the write is `critical=True` and its failure refuses the adoption,
    which the sibling tests cover. What this one pins is that the failure is reported
    rather than propagated -- hydration must not raise. Positive control below proves
    the call really was attempted, so this is not passing because nothing ran.
    """
    from unittest.mock import MagicMock, patch

    from kiro_crew.dashboard import chat_utils as cu

    fake = MagicMock()
    fake.log_governance_decision.side_effect = OSError("read-only file system")
    with patch.object(cu, "sel", return_value=fake):
        cu.audit_persisted_binding("slack_123", "slack:123", adopted=True)

    assert fake.log_governance_decision.call_count == 1


def test_the_gate_refuses_a_candidate_that_smuggles_a_literal_underscore():
    """A FOREIGN key whose own fold equals the transcript stem must be refused.

    This is the direction the sibling collision test does not cover: there the stem
    was the second ARGUMENT, here it is the transcript being hydrated and the
    candidate is a distinct live key that folds onto it. `_safe_key` is many-to-one,
    so `slack:C123_<ts>` folds to exactly the stem of `slack:C123:<ts>` -- adopting
    it would route later turns and saves into another session.

    The genuine spelling pair and the impostor differ in one measurable way: the
    impostor carries a LITERAL underscore where the real key carried a separator.
    """
    from kiro_crew.dashboard.chat_utils import persisted_binding_is_adoptable
    from kiro_crew.history import transcript_stem

    genuine = "slack:C123:1785370133.085469"
    impostor = "slack:C123_1785370133.085469"
    stem = "slack_C123_1785370133.085469"

    # Precondition: both really do fold onto the same stem, so this exercises the
    # measured collision rather than an imagined one.
    assert transcript_stem(genuine) == stem
    assert transcript_stem(impostor) == stem
    assert genuine != impostor

    assert persisted_binding_is_adoptable(genuine, stem), (
        "the genuine live key no longer adopts its own transcript -- a pruned map "
        "would drop the binding and the queued context with it"
    )
    assert not persisted_binding_is_adoptable(impostor, stem), (
        "a foreign session key was adopted because its own FOLD matched the "
        "transcript stem -- later turns would route through another session"
    )


def test_the_reverse_fold_is_not_accepted():
    """FINDING 3: a candidate that is merely the transcript key's fold is refused.

    That fold is many-to-one, so accepting it adopts a distinct session alias sharing
    one transcript file and channel/dashboard contexts diverge against one history.
    """
    from kiro_crew.dashboard.chat_utils import persisted_binding_is_adoptable
    from kiro_crew.history import transcript_stem

    live = "slack:C123:1785370133.085469"
    stem = transcript_stem(live)
    # Precondition: this really is the reverse shape the finding names.
    assert stem != live and transcript_stem(live) == stem

    assert persisted_binding_is_adoptable(live, stem), "the forward fold must still work"
    assert not persisted_binding_is_adoptable(
        stem, live
    ), "the reverse fold was accepted, adopting an ambiguous routing identity"


def test_a_substituted_separator_does_not_resolve_as_a_folded_binding():
    """GPT BLOCKING F1: `_safe_key` folds EVERY separator, so a shape check is not identity.

    ``_safe_key`` is ``re.sub(r"[^\\w\\-.]", "_", key)``, so ``:`` and ``/`` fold alike and
    ``slack/C123:<ts>`` shares a stem with the genuine ``slack:C123:<ts>``. An unanchored gate would
    ask only whether each folded position held some NON-UNDERSCORE character, which refuses
    an impostor smuggling a literal ``_`` but ADMITS one substituting another separator --
    and the value is agent-written metadata, so that is the adversary the gate exists for.
    Adopting it rebinds where the slot routes, under an alias the canonical key does not
    match.

    The pair is the point: the spoof must be refused AND the genuine spelling must still be
    adopted, because a gate that refuses both would silently unbind every channel slot.
    """
    from kiro_crew.dashboard.chat_utils import persisted_binding_is_adoptable
    from kiro_crew.history import _safe_key

    genuine = "slack:C123:1785370133.085469"
    stem = _safe_key(genuine)
    assert stem == "slack_C123_1785370133.085469", f"fold changed shape: {stem}"

    # POSITIVE CONTROL: the one legitimate two-spelling pair still resolves.
    assert persisted_binding_is_adoptable(
        genuine, stem
    ), "the canonical live key must still be adoptable for its own transcript"

    for spoof in (
        "slack/C123:1785370133.085469",
        "slack:C123/1785370133.085469",
        "slack C123:1785370133.085469",
        "slack@C123:1785370133.085469",
    ):
        assert _safe_key(spoof) == stem, f"{spoof} must collide to prove anything"
        assert not persisted_binding_is_adoptable(
            spoof, stem
        ), f"{spoof} substitutes a separator and must NOT resolve as {stem}'s binding"


def test_a_legitimate_literal_underscore_key_is_refused_and_that_is_measured():
    """A legitimate key carrying its own ``_`` IS refused, and that cannot be lifted here.

    ``discord:crew_agent:direct:user_1`` is refused for its own transcript, which unbinds a
    working session -- a real wrong outcome. It is unfixable at this call site because the
    legitimate spelling and the impostor are the same shape: admitting a literal ``_`` at a
    folded position also admits ``slack:C123_<ts>`` for the transcript of
    ``slack:C123:<ts>``, two DISTINCT live sessions sharing one stem, which the sibling test
    measures and refuses.

    Requiring the live separator at every folded position keeps the admissible spelling
    UNIQUE per stem. Fixing the false negative properly needs information the stem cannot
    carry, so it is a design change rather than a refusal tweak; this test exists so a later
    round cannot relax the character rule without confronting that.
    """
    from kiro_crew.dashboard.chat_utils import persisted_binding_is_adoptable
    from kiro_crew.history import _safe_key

    live = "discord:crew_agent:direct:user_1"
    stem = _safe_key(live)
    assert stem == "discord_crew_agent_direct_user_1", f"fold changed shape: {stem}"
    assert "_" in live, "precondition: the key carries a literal underscore of its own"

    assert not persisted_binding_is_adoptable(live, stem), (
        "the literal-underscore refusal has been lifted -- confirm the impostor "
        "slack:C123_<ts> is still refused for slack:C123:<ts>'s stem before accepting this"
    )

    # POSITIVE CONTROL: the all-separator spelling of the same conversation does adopt, so
    # the refusal above is the character rule biting rather than the fold check failing.
    canonical = "discord:crew:agent:direct:user:1"
    assert persisted_binding_is_adoptable(
        canonical, _safe_key(canonical)
    ), "the canonical spelling must still adopt its own transcript"


def test_the_binding_gates_accepted_spelling_closure_is_measured_per_channel_shape():
    """design + FP: the gate's accepted-spelling closure, measured against real channel shapes.

    FP's finding is correct that the cited grammar does not constrain a segment to `[\\w\\-.]`, so
    real ids carry `+` (WhatsApp E.164), `@` (a Teams thread, an iMessage handle) and literal `_`
    (`discord:crew_agent:...`), all of which fold to `_` in the stem and are therefore REFUSED when
    a hydration site presents that stem.

    Widening the predicate to accept every folded character was tried and is UNSAFE: it admits a
    SUBSTITUTED separator (`slack/C123:<ts>` adopting `slack_C123_<ts>`'s binding), which
    `test_a_substituted_separator_does_not_resolve_as_a_folded_binding` refuses. So this records the
    closure as it IS, per shape, rather than asserting a wider one. Closing the refusals needs the
    live spelling persisted beside the stem -- a state-format change, escalated, not a predicate
    tweak -- and this test is what will fail loudly on the day that lands.
    """
    from kiro_crew.dashboard.chat_utils import persisted_binding_is_adoptable
    from kiro_crew.history import transcript_stem

    # Adoptable: every folded position carries the grammar's own `:`.
    for key in (
        "slack:C123:1712345678.9001",
        "discord:123456789:direct:987654321",
        "telegram:-1001234567890:42",
    ):
        assert persisted_binding_is_adoptable(
            key, transcript_stem(key)
        ), f"{key} folds only at colons, so hydration must be able to adopt it"

    # Refused, measured: the folded position carries something other than a colon.
    for key, why in (
        ("whatsapp:+15551234567", "the + of an E.164 number"),
        ("imessage:someone@example.com", "the @ of an email handle"),
        ("teams:19:meeting_abc123@thread.v2", "a literal _ inside the thread id"),
        ("discord:crew_agent:direct:user_1", "literal _ in crew_agent and user_1"),
        ("webex:room_abc:thread_def", "literal _ in room_abc and thread_def"),
    ):
        assert not persisted_binding_is_adoptable(key, transcript_stem(key)), (
            f"{key} is measured as REFUSED ({why}). If this now passes, the gate widened -- move "
            "this shape into the adoptable group above and check the substituted-separator pin."
        )

    # The refusal is not free: an unbound slot answers from its own session, so a channel thread
    # stops seeing replies. That cost is what the escalation is about.
    assert (
        transcript_stem("whatsapp:+15551234567") == "whatsapp__15551234567"
    ), "the + folds to _, which is why the colon-only rule cannot see it as a separator"
