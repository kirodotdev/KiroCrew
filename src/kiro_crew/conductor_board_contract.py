"""The goal conductor's task board: the data shape its drawer template reads.

``kirocrew-conductor`` publishes its board with ``panel_publish`` and gets
``agent_panel_templates/kirocrew-conductor.html`` because the file is named for the
crew (``agent_panel.template_for_crew`` slugifies the crew name and looks for that
id). The conductor writes the data by hand each patrol turn, so unlike
``pipeline_board_contract`` there is no provider here: this module is only the
FORMAT, as a type, so the template and the skill text name one shape.

``test/test_conductor_board_contract_parity.py`` asserts the template reads exactly
the leaves of :class:`ConductorBoardPanel`, in both directions, and that the
template's state and step vocabularies equal :data:`TASK_STATES` and :data:`STEPS`.
"""

from __future__ import annotations

from typing import Final, NotRequired, TypedDict

#: The crew whose drawer renders this board, and the template id it slugifies to.
BOARD_CREW_NAME: Final = "kirocrew-conductor"
BOARD_TEMPLATE_ID: Final = "kirocrew-conductor"

#: A task's ``state``, as the conductor writes it. The template matches these
#: case-insensitively and reads ``_`` / ``-`` as a space, so ``needs_you`` and
#: ``Needs you`` both land on ``needs you``. Any other word still renders, in a
#: neutral pill.
TASK_STATES: Final = ("done", "working", "testing", "waiting", "needs you", "stuck")

#: A task's ``step``: where it is on the way to landed. Matched case-insensitively.
STEPS: Final = ("Code", "Test", "PR", "CI", "Done")


class ConductorBoardTask(TypedDict):
    """One work item on the board."""

    task: str
    state: str
    step: str
    #: A pull-request number or ``#N`` text. With the panel's ``repo`` it is a button
    #: asking the host to open the PR (``kirocrew-dashboard:open``); the sandbox has no
    #: ``allow-popups``, so a plain link would be a dead click. Without ``repo``, text.
    pr: NotRequired[str | int]


class ConductorBoardPanel(TypedDict):
    """The whole board, as ``panel_publish`` data."""

    #: The one thing only the user can unblock; empty when nothing needs them.
    needs_you: str
    #: ``"N of M"`` -- items accepted out of the round's total.
    done: str
    #: When the conductor last wrote the board, as display text.
    updated: str
    #: The conductor's next move, one line.
    next: str
    tasks: list[ConductorBoardTask]
    #: ``owner/name`` of the code host repo the ``pr`` numbers belong to. Optional.
    repo: NotRequired[str]
