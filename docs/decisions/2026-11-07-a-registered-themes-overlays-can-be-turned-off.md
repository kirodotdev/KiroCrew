# A registered theme's decorative overlays can be turned off, per theme, from the Experience section

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-07

## Decision

A theme registered through the branding seam that declares `overlays` gets one
Experience part, **Overlays**, in Settings > Display > Theme > Experience. It is
on by default, it is stored in the same per-theme record installed packs use, so
light and dark share it, and turned off it unmounts that theme's overlays. Its
top-bar elements, its CSS (such as a `body::before` image) and where its
overlays sit on screen are not part of it.

## Why

- The Bikini Bottom mascot, Karen, covers content on wide screens and a user
  had no way to hide her. The core mounts a registered theme's overlays, and
  the only Overlays switch was limited to installed theme packs.
- The switch reuses the existing Overlays toggle, its label and its record, so
  nobody's current view changes until they turn it off.
- A close button on an overlay itself, or moving it, stays possible and needs
  nothing from this switch.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/20542#issuecomment-6702847253 --
  the maintainer decision on the issue.
- https://github.com/kirodotdev/KiroCrew/issues/20542 -- the report and the
  three fixes it suggested; this is its first.
