# A meeting's sketch exports as one self-contained HTML file

Decided by: Bolin Chen (@bolichen97), maintainer
Date: 2026-11-08

## Decision
The meeting's More menu exports each diagram (sketch) agent's output as one self-contained HTML file built with the sketch frame's own sanitising and content policy, so its diagrams and tables both survive; an all-outputs bundle is a separate request, not part of this export.

## Why
One HTML file keeps the tables that an image of the diagrams would drop and stays inside the frame's existing protections, and a zip bundle would need a new frontend dependency.

## Evidence
- https://github.com/kirodotdev/KiroCrew/issues/21180#issuecomment-6717665224 (the decision)
- https://github.com/kirodotdev/KiroCrew/issues/21180 (the request)
- https://github.com/kirodotdev/KiroCrew/pull/30863 (the notes export this one sits beside)
