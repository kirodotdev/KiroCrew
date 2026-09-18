def test_the_generic_metadata_read_does_no_sidecar_io(tmp_path, monkeypatch):
    """Folding on every metadata read put sidecar I/O on ~25 call sites, several async.

    Offloading the resume handler was not enough: ``get_metadata`` is called from telemetry,
    sessions, mcp_tools, slack and the projection, so the fold has to be OPT-IN. Only hydration
    and save-accounting need the spill re-attached, and those reads are sync or already
    offloaded, so the folding accessor is where the cost belongs.
    """
    from kiro_crew import history as h

    log = h.ConversationLog(tmp_path)
    key = "chat-optin-fold"
    log.append(key, "user", "a turn")
    log.update_metadata(key, {"pending_context": []})
    h.write_ctx_overflow(key, [{"ctxId": "spill-1", "content": "x"}], tmp_path)

    reads: list[str] = []
    real = h.read_ctx_overflow
    monkeypatch.setattr(h, "read_ctx_overflow", lambda k, b=None: (reads.append(k), real(k, b))[1])

    log.get_metadata(key)
    log.get_metadata_status(key)
    assert reads == [], (
        f"the generic metadata accessors read the sidecar {len(reads)} time(s); every caller "
        "pays that I/O, including async ones that never offloaded it"
    )

    folded = log.get_metadata_with_overflow(key)
    assert reads, "the opt-in accessor must still fold, or hydration loses the spill"
    assert "spill-1" in {
        e.get("ctxId") for e in folded.get("pending_context", []) if isinstance(e, dict)
    }


def test_the_folding_read_takes_one_snapshot_of_line_and_sidecar(tmp_path, monkeypatch):
    """The line and the sidecar are two files, so an unlocked fold can straddle a save.

    A save that moves an entry off the metadata line and into the sidecar writes both files. A
    reader holding no lock can read the line BEFORE that move and the sidecar AFTER it, and the
    moved entry is then in neither half of what it assembles -- an acknowledged entry lost to an
    ordinary interleaving rather than to a crash.

    Asserted by BLOCKING: the fold is held open mid-read while another thread tries to take the
    same key's lock, and that thread must not get in. A competitor that acquires while a fold is
    in progress is exactly the window the loss needs.
    """
    import threading

    from kiro_crew import history as h

    log = h.ConversationLog(tmp_path)
    key = "chat-one-snapshot"
    log.append(key, "user", "a turn")
    log.update_metadata(key, {"pending_context": [{"ctxId": "on-line", "content": "a"}]})
    h.write_ctx_overflow(key, [{"ctxId": "spilled", "content": "b"}], tmp_path)

    entered = threading.Event()
    release = threading.Event()
    real_read = h.read_ctx_overflow

    def _hold(k, base=None):
        entered.set()
        release.wait(timeout=10)
        return real_read(k, base)

    monkeypatch.setattr(h, "read_ctx_overflow", _hold)

    acquired: list[bool] = []
    joined: list[bool] = []

    def _competitor() -> None:
        if not entered.wait(timeout=10):
            return
        got = threading.Event()

        def _try() -> None:
            with log._locked(key):
                got.set()

        t = threading.Thread(target=_try, daemon=True)
        t.start()
        acquired.append(got.wait(timeout=0.75))
        release.set()
        # BOUNDED JOIN: `t` is still blocked on the lock the reader holds, and the moment it gets in
        # it mkdirs the lock's parent -- after teardown that RECREATES the removed tmp_path.
        t.join(timeout=10)
        joined.append(not t.is_alive())

    folded: list[dict] = []

    def _fold() -> None:
        folded.append(log.get_metadata_with_overflow(key))

    reader = threading.Thread(target=_fold)
    comp = threading.Thread(target=_competitor)
    reader.start()
    comp.start()
    comp.join(timeout=15)
    reader.join(timeout=15)

    assert joined == [True], (
        "the lock thread outlived the test: once it acquires the lock it mkdirs the lock's parent, "
        "recreating tmp_path after teardown removed it and leaving a stray directory behind"
    )
    assert acquired and acquired[0] is False, (
        "another writer took the key's lock WHILE the fold was mid-read, so the metadata line "
        "and the sidecar are read as two snapshots and an entry moving between them is lost"
    )
    assert folded, "the folding read did not complete"
    ids = {e.get("ctxId") for e in folded[0].get("pending_context", []) if isinstance(e, dict)}
    assert ids == {"on-line", "spilled"}, f"the fold must still return both halves, got {ids}"


def test_the_fold_never_writes_on_a_read(tmp_path):
    """A read-modify-write in the fold races a concurrent close and erases its spill.

    An earlier revision pruned the sidecar from inside the fold. That read can already be stale,
    so the rewrite replaced entries a close had just written and the close then committed without
    them -- permanent loss of acknowledged context. The retry belongs to the SAVE, which owns the
    write, and the test below proves the save still performs it.
    """
    import inspect

    from kiro_crew import history as h

    src = " ".join(inspect.getsource(h._fold_ctx_overflow).split())
    assert "sync_ctx_overflow" not in src and "write_ctx_overflow" not in src, (
        "the fold still writes on a read path, so a stale read can replace a concurrent "
        f"close's spill: {src[:160]}"
    )
    # CONTROL: the fold must still be the thing that re-attaches the spill, or this would pass
    # for a fold that had simply stopped folding.
    assert "read_ctx_overflow" in src


def test_the_resume_handler_folds_the_spill_on_every_metadata_read(tmp_path):
    """The final reread REPLACES ``meta``, so an unfolded read drops the spill for that turn.

    ``meta = post_read_meta`` hands the reread's value to ``restore_pending_context``, and the
    identity barrier compares the two snapshots -- so a folded first read against an unfolded
    reread both loses context AND makes the barrier refuse whenever a sidecar exists.
    """
    import inspect

    from kiro_crew.dashboard import chat_handlers as ch

    src = " ".join(inspect.getsource(ch.api_chat_slot_resume).split())
    unfolded = src.count("state.conversation_log.get_metadata,")
    unfolded += src.count("state.conversation_log.get_metadata_status,")
    folded = src.count("get_metadata_with_overflow") + src.count(
        "get_metadata_status_with_overflow"
    )
    assert src.count("meta = post_read_meta") == 1, "the reread no longer replaces meta"
    assert unfolded == 0, (
        f"{unfolded} resume metadata read(s) still use the NON-folding accessor while the "
        f"handler hydrates from their result ({folded} fold)"
    )
