# Incognito and Temporary chats keep their transcript in History; the modes promise "learn nothing", not "leave no disk record"

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-09-25

## Decision

An Incognito or Temporary dashboard chat writes its transcript to the session
history like any other chat, so the user can reopen it before and after a
restart. What the mode withholds is everything DERIVED from the chat --
consolidation, lessons, memory injection, the session summary, suggestions,
export and cross-instance transfer -- and every reader that derives gates on
the `memory_mode` the transcript's metadata line carries. Pull request #13760
carries this direction; pull request #13695, which kept the modes non-persistent
and relabelled them "Not saved", is closed as superseded.

## Why

- The two modes exist so a chat teaches the product nothing. Refusing the
  transcript write (introduced by #11780) added no privacy the readers' own
  `memory_mode` gate did not already give; it only lost the chat, silently, for
  anyone whose default mode was Incognito or Temporary -- while the mode picker
  and Settings still said the chat was kept.
- Given the two directions on record (#13760 persist-and-gate, #13695 keep
  non-persistence and relabel), the maintainer chose #13760 on 2026-09-25 and
  closed #13695. Test docstrings from #11780 that read "a transient conversation
  is not persisted merely to allow restart" describe the reversed behaviour and
  are retired by #13760; they were never a decision record.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/13760 -- the pull request carrying
  the direction and this entry.
- https://github.com/kirodotdev/KiroCrew/pull/13695#issuecomment-5835321690 --
  the closing comment on the superseded pull request, posted on the maintainer's
  instruction on 2026-09-25 ("Closing per bolichen's decision").
- https://github.com/kirodotdev/KiroCrew/pull/11780 -- the change that made the
  modes non-persistent, without a decision record.
