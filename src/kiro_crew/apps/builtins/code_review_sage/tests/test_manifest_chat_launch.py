"""The manifest's ``permissions.chatLaunch`` declares exactly the follow-up chat the page opens.

The review panel opens a follow-up session through an app launch
(``POST /api/chat/slots`` with ``app_launch``), and the gateway admits it only inside
this declaration. So the declaration must cover every follow-up the app can hand the
page, or a follow-up is refused: the slot key ``followup.slot_key`` derives, both
agents a review can run as, the folder ``_ensure_followup_folder`` files it under, and
the memory mode the page sends. And it must declare nothing more.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from sage_lib import followup as FU  # noqa: N812
from sage_lib import review_pool as RP  # noqa: N812

from kiro_crew.apps.chat_launch import parse_chat_launch

MANIFEST = Path(__file__).resolve().parents[1] / "app.json"


def _declared():
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))["permissions"]["chatLaunch"]
    launch = parse_chat_launch(raw)
    assert launch is not None, "permissions.chatLaunch does not parse"
    return launch


class TestManifestChatLaunch(unittest.TestCase):
    def test_every_follow_up_key_is_under_the_declared_prefix(self):
        launch = _declared()
        for run_id, change_id in (("run1", "GH-o-r-42"), ("r/../x", "GH-o-r-1 ../.."), ("", "")):
            self.assertTrue(launch.covers_key(FU.slot_key(run_id, change_id)), run_id)

    def test_both_review_agents_and_nothing_else_are_declared(self):
        self.assertEqual(set(_declared().agents), {RP.REVIEW_AGENT, RP._FALLBACK_AGENT})

    def test_the_folder_and_memory_mode_are_the_ones_the_follow_up_uses(self):
        launch = _declared()
        self.assertEqual(launch.folder, FU.FOLDER_NAME)
        # ReviewChat.tsx sends 'persistent' on every open and continue.
        self.assertEqual(launch.memory_modes, ("persistent",))


if __name__ == "__main__":
    unittest.main()
