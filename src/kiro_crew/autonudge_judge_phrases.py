"""The clauses a user-facing text uses for what a gated loop's wake depends on.

Split from :mod:`kiro_crew.autonudge_judge`, which serves both names, so a tool description
can quote them without loading the judge: a tool server writes its descriptions before it
loads the code that runs its tools.
"""

from __future__ import annotations


def screen_phrase() -> str:
    """What a gated loop's wake depends on, as one clause for a user-facing text.

    Named from the SCREEN rather than from a fixed set of signals, because there is
    no fixed set any more: every tick is read against the loop's own wake criteria,
    or against the shipped default when it named none. A text that spelled out a
    list of wake reasons would be promising a rule the judge does not follow.

    The second clause is there because the screening is not always available: a loop
    that names no criteria is screened only where its owner granted this point's
    egress scope, and one that names its own needs a judge lane armed. Neither holds
    on a stock install, and a promise that every tick is screened would be read as
    covering exactly the machine where it is not. What is still free there is a
    subject that did not change, which the reading answers on its own.
    """
    return (
        "every tick is read against your own wake criteria, or the shipped default "
        "brief when you name none, so progress that does not need you costs no turn; "
        "where no judge lane is available only an unchanged subject is free, and "
        "anything else spends the turn"
    )


def ending_phrase() -> str:
    """What ENDS a watch, as one clause. Capitalised to open a sentence.

    One mapping, and it is deterministic: the judge cannot end a watch, because
    ending one is the verdict an owner cannot recover by waiting and the judge reads
    text a third party wrote.
    """
    return "A merge or a close"
