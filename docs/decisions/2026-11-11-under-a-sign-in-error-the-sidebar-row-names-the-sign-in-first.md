# Under a sign-in error the sidebar row names the sign-in first

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-11

## Decision

When a chat's interrupted turn ended on a sign-in error, its sidebar row reads
"Turn interrupted · Sign in, then Resume" instead of "Turn interrupted · Resume";
the header's "Resume N sessions" is unchanged.

## Why

- The row gives the same advice as the composer's hint under the same error,
  and stays true once the sign-in lands.
- Dropping signed-out chats from "Resume N sessions" would make recovery after a
  sign-in go one chat at a time.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/32065#issuecomment-6798328371 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/32065 -- the issue.
