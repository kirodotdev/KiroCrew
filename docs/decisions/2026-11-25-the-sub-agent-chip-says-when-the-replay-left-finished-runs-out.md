# The sub-agent chip says when the replay left finished runs out

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-25

## Decision

After a reload, the gateway sends the dashboard, per chat, how many finished sub-agent runs the bounded replay left out, and the sub-agent progress chip says so in its header: with the number when it is exact, and without a number, not calling the runs older, when it is only a lower bound; a chip whose replay left nothing out is unchanged, and an install with the crew log switched off keeps the server warning only.

## Why

- The chip counts the cards the browser holds, so after a reload its done count falls to the live runs plus the newest 50 finished ones, and nothing told the user that more exist.
- The count was kept off the wire because no client acted on it; the chip now does, so that reason no longer holds.
- The totals, the 50-run and 24-hour bounds and the server warning stay as they are, and a browser that does not know the new message renders as before.

## Evidence

- Maintainer decision: https://github.com/kirodotdev/KiroCrew/issues/42636#issuecomment-7173375818
- Issue: https://github.com/kirodotdev/KiroCrew/issues/42636
