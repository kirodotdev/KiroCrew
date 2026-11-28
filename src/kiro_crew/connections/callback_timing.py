"""How long a Crew OAuth consent callback holds its page, and how long its relay waits.

Its own module so the dashboard's return-address relay reads the wait without
loading :mod:`kiro_crew.connections.crew_oauth`, which stays imported lazily.
"""

from __future__ import annotations

#: How long the consent tab waits for the code swap and the write before it
#: answers that the outcome is shown in Kiro Crew instead.
_CALLBACK_PAGE_WAIT_SECS = 45.0
#: How long the relay waits for the callback's status line. A Kiro Crew sign-in
#: holds its page until the code swap and the write settle, so this outlasts
#: that page wait.
_CALLBACK_STATUS_TIMEOUT_SECS = _CALLBACK_PAGE_WAIT_SECS + 10.0
