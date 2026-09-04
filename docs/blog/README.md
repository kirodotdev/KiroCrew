# Blog

Essays about where agent-assisted work is going and why Kiro Crew is built the
way it is. These are arguments rather than contracts: nothing here is normative,
and no code change is required to keep a post true.

That is the line between this directory and its neighbours. A
[request-for-change](../request-for-change/README.md) proposes a specific change
and records a decision. A [system spec](../system-specs/README.md) is a
change-control contract a code change must update in the same commit. A post here
is a position. You can argue with it, and it can age.

## Index

| Post | What it argues |
|---|---|
| [vibe-coding-generations.md](vibe-coding-generations.md) | The 1.0-to-4.0 ladder measures the wrong variable. What gates each step is blast radius, 3.0 is oracle engineering rather than a better session list, and accountability moves into an agent system as permission tied to a record the agent cannot write, not as a conscience. |

## Writing one

- File as `<topic>.md`, kebab-case, English.
- Open with front matter (`title`, `author`, `created`), then an H1, then the
  argument. No `Last Updated:` line, because git already records that.
- Add a row above, and keep the "what it argues" column a claim rather than a
  topic. A reader should be able to disagree with it from the index alone.
- Cite `file:line` and name the commit for any claim about the code, the same as
  an RFC does. A post is allowed to be opinionated about the future. It is not
  allowed to be wrong about the present.
- Say which parts are unbuilt. If a post reads as a roadmap commitment, it
  belongs in `request-for-change/` instead.
