"""Limits and file names of the Mastermind memory tree."""

from __future__ import annotations

#: The longest line of the tree, in UTF-8 bytes. A message or a pair of lines
#: that fits is its own node; a longer one is summarized to fit.
NODE_BYTES = 512

#: The last messages a turn also gets whole, after the view: at most this many,
#: newest first, within RECENT_BYTES (a longer message is cut to fit).
RECENT_MESSAGES = 6
RECENT_BYTES = 16_000

#: The bytes of a turn's view past which it merges its oldest lines down to
#: half, so a view stays between half and all of it. A turn sizes it to the
#: model's context window (``view_high_for``); this is the size before a turn
#: has told it one, and the floor.
VIEW_HIGH_BYTES = 128_000

#: The share of the model's context window a turn's view may fill. A turn is
#: an agent turn: its own tool calls and results fill the same window, and the
#: session compacts at ``session.autocompact_pct`` (70% by default), so the view
#: leaves well over half of it for the turn's work. Recall over many facts also
#: degrades as a window fills, and the view is sent whole on every turn.
VIEW_WINDOW_SHARE = 0.25

#: The most a turn's view may hold, whatever the window: past ~250K tokens a
#: larger view costs every turn more than it adds, as zoom reaches the rest.
VIEW_MAX_BYTES = 1_000_000

#: UTF-8 bytes per token of summary text, to turn a window into bytes.
BYTES_PER_TOKEN = 4

#: The window assumed when the model's is unknown (``auto``, not reported yet).
DEFAULT_WINDOW_TOKENS = 200_000

#: The same for the view a summary call reads as context.
SEEN_HIGH_BYTES = 32_000

#: How many summaries are written at once.
MAX_PARALLEL_SUMMARIES = 4

#: Tries at a summary that comes back over NODE_BYTES before the shortest is
#: kept.
SUMMARY_TRIES = 5

#: Seconds before a summary whose call failed (backend down, signed out) is
#: asked again. The tree only waits on it; nothing is lost.
SUMMARY_RETRY_SECS = 60.0

#: The characters of a message one ``zoom`` returns; a longer one comes in
#: pages from character ``at``.
ZOOM_PAGE_CHARS = 20_000

#: The data home's directory for the tree, and its files.
MASTERMIND_DIR_NAME = "mastermind"
MESSAGES_FILE = "messages.jsonl"
NODES_FILE = "nodes.jsonl"
VIEWS_FILE = "views.json"

#: The kinds of message the tree keeps: the person's words, the agent's
#: replies, and automation's messages (a cron, a subagent's report).
KIND_USER = "user"
KIND_AGENT = "agent"
KIND_EVENT = "event"

#: The file whose presence turns Mastermind on, in the tree's directory.
ENABLED_FILE = "enabled"
