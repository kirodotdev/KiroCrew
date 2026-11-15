# A chat send is never told it lost to a send that was not accepted

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-10

## Decision

A chat send is never told it lost to a send that was not accepted, including
while another send to the same session is still pending.

## Why

- A pending send may still be released and never land, so naming it the winner
  tells the sender about a message that does not exist.
- The Shared Crew spec already promises that a refused send never makes a
  collaborator lose to nothing; this keeps that promise for a send that waited
  out the settle wait.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/34412#issuecomment-6769893267 --
  the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/34412 -- the issue.
