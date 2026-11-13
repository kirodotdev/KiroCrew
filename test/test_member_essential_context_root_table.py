"""A member turn reads its managed-root table once per document read, not once per path.

Every warm turn of a V2 crewmate DM rebuilds the essentials envelope, and every
path that build checks went through ``_refuse_managed_source``, which re-read the
config and re-resolved every admin and workspace root each time: ~37 checks and
~0.6 s per turn on a real store. The table is now read once per
``documents_for_member`` call, and read again on the next turn.
"""

from collections import Counter

import member_essential_context_helpers

from kiro_crew import member_essential_context as mec

_close_skills_loaders = member_essential_context_helpers._close_skills_loaders
env = member_essential_context_helpers.env


def _count_root_reads(monkeypatch):
    calls: Counter = Counter()
    reads = {"documents": 0}
    real_root = mec._comparable_root
    real_documents = mec.documents_for_member

    def counting_root(root):
        calls[root] += 1
        return real_root(root)

    def counting_documents(*args, **kwargs):
        reads["documents"] += 1
        return real_documents(*args, **kwargs)

    monkeypatch.setattr(mec, "_comparable_root", counting_root)
    monkeypatch.setattr(mec, "documents_for_member", counting_documents)
    return calls, reads


def _warm_turn(env):
    message, _ = env.builder.build_message(
        "Continue",
        False,
        "dashboard:member-writer",
        memory_store=env.store,
        member=env.member,
        project=str(env.project),
    )
    return message


def test_a_warm_member_turn_resolves_each_root_once_per_document_read(env, monkeypatch):
    calls, reads = _count_root_reads(monkeypatch)
    message = _warm_turn(env)
    assert "Project rules: run the review checks." in message
    assert reads["documents"] >= 1
    assert calls, "the managed-source check must still resolve its roots"
    # One table per documents_for_member call: each root resolved once per read.
    assert max(calls.values()) <= reads["documents"], calls


def test_the_next_turn_reads_the_roots_again(env, monkeypatch):
    calls, reads = _count_root_reads(monkeypatch)
    _warm_turn(env)
    first = sum(calls.values())
    _warm_turn(env)
    assert sum(calls.values()) == 2 * first > 0


def test_a_managed_source_is_still_refused_inside_one_read(env):
    managed = mec.config_dir() / "members" / "writer" / "SOUL.md"
    with mec._managed_roots_scope():
        try:
            mec._refuse_managed_source(managed)
        except mec._ManagedEssentialSourceError:
            pass
        else:
            raise AssertionError("managed member state was admitted as a project resource")
