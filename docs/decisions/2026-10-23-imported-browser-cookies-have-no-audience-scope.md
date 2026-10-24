# Imported browser cookies reach every agent browser, with no audience scope

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-10-23

## Decision

Cookies the owner imports from the dashboard are delivered to every agent browser on the gateway, including browsers that serve non-owner members and restricted sessions; this exposure is accepted.

## Why

- The gateway finds agent browsers by their control socket, and a browser is not tied to one audience: agent processes are pooled across sessions, so one browser can serve the owner and a member in turn.
- Scoping delivery to owner sessions needs a session to process to browser mapping that the launch path does not have. That is a separate change with its own design.
- Non-owner members (Slack/Discord allow-list) and restricted (incognito/temporary) sessions can therefore browse as the owner on the imported sites. Clear cookies stops further use; it does not undo a page already read.
- The acceptance covers this exposure only. It does not clear any other review finding.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/23332 -- the pull request that adds cookie import and this entry.
- https://github.com/kirodotdev/KiroCrew/pull/23332#issuecomment-6377799382 -- the maintainer restates the ruling on the pull request.
- `docs/system-specs/modules/browser.md` -- residual (iii) in the imported cookies section.
