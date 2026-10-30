# No scrollbar in the dashboard is visible at rest

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-10-23

## Decision

A scroll box in the dashboard does one of two things with its scrollbar: it hides it entirely, or it shows a thin thumb only while the box is being scrolled, in a column the box reserves anyway so revealing the thumb moves no content. A scrollbar that is visible while nothing scrolls is never allowed, on any surface, for any pointer.

## Why

- The crewmate chat painted a permanent 6px thumb down the transcript's right edge. A permanently scrollable region turns a thumb into a fixed stripe, not a transient hint; the sessions sidebar had already hidden its own for that reason.
- Three treatments coexisted with no rule deciding which a surface got: the global always-visible thumb, the sidebar's fully hidden one, and the hover-revealed overlay on capped lists and code blocks. Every new surface made its own call.
- The maintainer's requirement, in their words: avoid a scrollbar that can be seen at any time. Fully hidden and shown-while-scrolling both meet it; the maintainer named both as the allowed forms and asked that a reveal not change the box's width. Hover-reveal is not one of them: a pointer rests over the chat transcript most of the time, so a hover-shown thumb is an always-shown thumb there.
- Out of scope: the Mochi app's own windows, which do not read the dashboard stylesheet.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/23539 -- the issue recording the request and the decision.
- https://github.com/kirodotdev/KiroCrew/pull/23550 -- the change that applies it; the maintainer restates the two allowed forms in its thread.
