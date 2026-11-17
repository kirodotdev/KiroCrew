# The MCP servers list names a config file it skipped for its encoding

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-11

## Decision

The MCP servers list on the Connections page shows one error line above the
list for each MCP config file it skipped because the file is saved as UTF-16
or UTF-32, with or without a byte-order mark for UTF-16, naming the file and
the same fixed "re-save it as UTF-8" reason the chat's session-start notice
gives.

## Why

- A skipped file adds no rows, so without the line its servers are just
  missing and only the gateway log says why.
- The session-start notice, the gateway log and this line name the same file
  and the same fix, from one shared reason.
- Only an encoding refusal gets the line; a file that is not valid JSON is a
  different problem.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/34150#issuecomment-6784359763 --
  the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/34150 -- the issue.
- https://github.com/kirodotdev/KiroCrew/issues/34205 -- the follow-up it
  closes too.
