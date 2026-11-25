# A Bot's page snapshot also hands back the whole page's text; it does not read a person's open tab

Decided by: Bolin Chen (@bolichen97), maintainer
Date: 2026-11-14

## Decision
`dashboard_snapshot` also returns the rendered text of the whole applied page, with its bound values and the frame's own "not current" and "may be out of date" marks, read in the same offline run over the Bot's real data, and it does not read the DOM of a tab the person has open.

## Why
- The issue asks that a Bot can check its own page, below the first screen and for a stale value, without a person's screenshot.
- The offline run already gets the read and the redaction the tab gets; reading an open tab needs a tab to be open and a new channel from the browser.

## Evidence
- https://github.com/kirodotdev/KiroCrew/issues/36935
- https://github.com/kirodotdev/KiroCrew/issues/36935#issuecomment-6877818162
