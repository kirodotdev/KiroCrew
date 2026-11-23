# A session dropped on a Split View pane shows in that pane

Decided by: Bolin Chen (@bolichen97), maintainer
Date: 2026-11-14

## Decision
In Split View, dropping a session from the sessions list onto a pane shows that session in that pane, and a session already shown in a pane is refused with a visible note; the edge-band split asked for alongside it is not part of this decision, and the Board layout and the single-chat drop stay as they are.

## Why
"Replace is the main ask here and needs one drop target per pane; the edge split can follow as its own request once replace has shipped."

## Evidence
- https://github.com/kirodotdev/KiroCrew/issues/36883#issuecomment-6877436590 -- the maintainer decision.
- https://github.com/kirodotdev/KiroCrew/issues/36883 -- the request.
- https://github.com/kirodotdev/KiroCrew/issues/18881 -- the earlier split request.
