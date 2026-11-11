"""The texts Mastermind gives the model: how to read the view, and the
summary tasks."""

from __future__ import annotations

from kiro_crew.mastermind.constants import NODE_BYTES

#: A line not summarized yet, as the view shows it.
NOT_SUMMARIZED = "(not summarized yet: zoom it)"

# A ruler, not a sample line: a model given a sample copies its content.
_RULER = "-" * NODE_BYTES

VIEW_GUIDE = """[MASTERMIND MEMORY]
This is Mastermind, one chat with the user that never ends. You do not get
the chat's earlier turns as messages: you get its memory, the view below,
and the user's new message after it.

The view: the whole chat, oldest first, inside <chat> tags, as one-line
summaries. Each line is

  id+n|text   the n messages from id on, summarized (newlines as spaces)

Each message has a kind: user (the user's words), agent (your replies), or
event (automation: a cron job, a subagent's report; not the user's words).
The summaries form a binary tree: each message is compressed into a line (a
short message is its own line), then adjacent lines are merged in pairs, again
and again. So recent lines cover one message each and older lines cover more.
After the view, <recent> holds the last few messages whole (a long one cut).
A message not summarized yet shows as "(not summarized yet: zoom it)".

The view is your memory, and its latest word on a thing is the truth. Use the
mastermind_zoom tool: zoom(id, n) opens line id+n into the two lines it was
made from, and zoom(id, 1) gives message id whole. Zoom whenever a summary
only mentions something you need, such as what your last reply said, a
decision, a past attempt or where a file is, before you act, guess or ask.
mastermind_date(id) gives the date and time of message id.
[END MASTERMIND MEMORY]"""

LEAF_TASK = f"""Compaction: compress message {{id}} into one line of at most {NODE_BYTES} bytes
(about 70 words), the length of this ruler:
{_RULER}
Keep what a later reader would need to find it again: names, files, numbers,
decisions, open questions. Reply with the line alone.
<input>
"""

MERGE_TASK = f"""Compaction: merge lines {{a}} and {{b}}, adjacent, into one line of at most
{NODE_BYTES} bytes (about 70 words), the length of this ruler:
{_RULER}
<chat> may hold their messages, {{id}} to {{end}}, in more detail: take details
of them from there too. Reply with the line alone.
<input>
"""

SUMMARY_SYSTEM = """You write the summaries of a chat's memory tree. The chat so far is
inside <chat> tags as one-line summaries (id+n|text: the n messages from id
on). Do exactly the task after it and reply with the one line it asks for,
nothing else. You are not the chat's agent: write of it, and of the user, in
the third person."""


def over_task(line: str, cut: str) -> str:
    """Ask again for a line that came back too long, showing where it must end."""
    size = len(line.encode("utf-8"))
    return (
        f"Too long: your last line for this <input> was {size} bytes, over the\n"
        f"{NODE_BYTES}-byte limit. Write the whole line again, cutting just enough of\n"
        f"the least valuable items to fit before this cut:\n{cut}| <- LIMIT"
    )
