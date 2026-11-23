# A PostToolUse hook is told when its tool output was cut

Decided by: Bolin Chen (bolichen97), Kiro Crew maintainer
Date: 2026-11-22

## Decision

Where Kiro Crew fires `PostToolUse` itself, `tool_response` carries `output` (the redacted tool output cut to its first 2,000 characters) plus `truncated` and `output_length` (the length of the redacted text `output` was cut from), and the hooks reference documents the cap and both keys.

## Why

- A hook that parses a long tool result got the first 2,000 characters with no sign anything was missing, so a cut result could not be told from a short one.
- The two keys are additive: a hook that reads only `output` keeps working, and the 2,000-character bound stays as it is.
- Each site measures the redacted text it hands the hook. Kiro Crew holds a tool result it reads over ACP to no more than about 8,000 characters, so a longer one reports that shortened length.
- Options not chosen: a documentation line alone (a hook still could not tell per call) and a marker inside `output` (it would break the pinned lengths and make a cut JSON result harder to recover).

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/38593#issuecomment-7076466873 -- the decision, as a maintainer listed in `MAINTAINERS.md`.
- https://github.com/kirodotdev/KiroCrew/issues/38593 -- the report.
