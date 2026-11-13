# An SVG artifact keeps its own stylesheet, drawn in a shadow root of its own

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-08

## Decision

The SVG viewer and the library card draw a sanitized SVG artifact inside a
shadow root of its own and keep its `<style>` elements there, rather than
removing them, so an SVG styled through its own stylesheet renders as authored
and that stylesheet still cannot restyle the dashboard, on screen or in print.

## Why

- Removing the stylesheet left design-tool and agent-written SVGs with default
  black fills; a note saying it was removed would explain the loss without
  undoing it.
- A shadow root keeps the stylesheet to the SVG, which is what the earlier fix
  needed.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/32575#issuecomment-6719768853 --
  the maintainer's ruling between the issue's two options.
- https://github.com/kirodotdev/KiroCrew/issues/32575 -- the issue.
- https://github.com/kirodotdev/KiroCrew/issues/31600 and
  https://github.com/kirodotdev/KiroCrew/pull/32238 -- the stylesheet removal it
  replaces.
