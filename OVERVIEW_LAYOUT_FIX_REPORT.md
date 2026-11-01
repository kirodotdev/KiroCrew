# Kiro BYO 0.2.2 — Overview Two-Column Layout Fix

## Baseline and scope
- Baseline: the most recent user-visible `kiro_cancel_chart_delivery/Kiro_BYO_0.2.2-source.zip` (prior 0.2.2 cancellation + chart fix).
- Changed runtime file: `ui/index.mjs` only. Version remains `0.2.2`.
- No changes to API/provider request processing, ACP/Mode/Type logic, existing language resources, saved user state, cancellation classification, or telemetry.

## Changes
1. The overview grid is responsive **to its embedded content width**, not the host desktop viewport. At typical 1555/1440 desktop widths: 2.05fr left performance graph, 0.95fr right provider health (with a 240px lower bound).
2. Both panels share the remaining overview height without introducing page scroll. Provider health card header stays pinned, while its body has local vertical overflow with a stable scrollbar gutter.
3. The chart uses a ResizeObserver on its local container to match its SVG axes and points to the real plot dimensions; no interpolated/fictional telemetry is created. When viewport space is insufficient, the panels stack and the overview can scroll vertically.
4. Regression test `tests/test_overview_two_column_layout.py` measures box geometry, chart space and scrolling for 2 and 25 simulated provider rows at 1555/1440/1300/1200/1100px viewport widths. Updated `tests/test_trend_and_cancel_ui.py` to enforce the new actual layout contract.

## Limits
- Chromium test uses KiroCrew-like host layout and actual shipping CSS; the real KiroCrew WebView/build has not been exercised in this container.
- Automatic browser layout tests do not prove every translated string layout or zoom level. At narrow inner widths the design intentionally stacks and permits vertical scrolling instead of squeezing text.
- The preview screenshot uses simulated providers/data for presentation, not actual model telemetry.
