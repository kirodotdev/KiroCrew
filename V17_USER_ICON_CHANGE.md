# Kiro BYO 0.3.0 V17 — user-provided SSSP-BYO emblem

This is an **icon-only update** based on the V16 source archive. The image content is **user supplied**.

- Source: `assets/logo_bw_text.png`, a byte-identical copy of the supplied transparent 746×324 PNG.
- Crew library card: `assets/icon.png`, 512×512 opaque PNG with an unframed white background and the complete logo at original aspect ratio.
- Crew sidebar: `ui/icon.svg`, a self-contained SVG wrapper embedding the original PNG; a dark-mode color inversion is applied for legibility.
- BYO internal header: `ui/index.mjs` now uses the same `ui/icon.svg` from the module URL rather than an unrelated `K` tile.
- Existing `app.json.iconPath` and `ui.pages[].iconUrl` remain unchanged. No Mode, Harness, Provider, source-audit or storage logic changed.

**Validation boundary:** SVG rendering was checked in local Chromium with the image embedded as data; this is not a full KiroCrew desktop visual check. App reload or cache refresh may be required for icon changes to appear.
