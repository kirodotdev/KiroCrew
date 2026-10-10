# Screenshots for the v3 Dashboard tab (#18642)

Evidence branch for the frontend PR of the Dynamic Dashboard v3 stack. It
carries images only, so the implementation branch stays free of binaries.

Grouped by functional requirement. Each caption says what the shot proves and,
where it matters, what it does NOT prove.

| file | requirement | what it shows |
|---|---|---|
| `shots/R1-package-read-doc-light.png` | R1 | The tab reads a dashboard artifact's layout package and draws from it, light theme. |
| `shots/R1-package-read-doc-dark.png` | R1 | The same page in the dark theme, so the theme tokens are shown to be real rather than hard-coded. |
| `shots/R2-empty-state.png` | R2 | A member with no package gets an empty state. There is no composed default page. |
| `shots/R3-no-fallback.png` | R3 | The tab refuses a page it was sent rather than falling back to a template. **Frontend half only**: the route is stubbed, so this is the tab declining, not the server declining to send. |
| `shots/R3b-no-fallback-package-bound.png` | R3 | The same refusal on the PACKAGE-BOUND body shape, which is the one a widened or inverted condition would break. Frontend half only, as above. |
| `shots/R4-will-not-compose.png` | R4 | The body carries a full dashboard and the tab still shows the empty state, so no default page is composed from available data. |
| `shots/R5-patch-before-doc-light.png` | R5 | Before a fold push, light theme. |
| `shots/R5-patch-after-doc-light.png` | R5 | After the fold push: only the blocks that subscribe to the fold moved, and each value is formatted by the renderer's own rules. |
| `shots/R5-patch-before-doc-dark.png` | R5 | The same before state, dark theme. |
| `shots/R5-patch-after-doc-dark.png` | R5 | The same after state, dark theme. |

What is real in these captures and what stands in: the component, the
providers, the theme tokens and the i18n catalogs are real; the controller
response, the mint and the host document are stand-ins. The document implements
the renderer's own listener contract, so an R5 pair proves the host posts the
right shape and a conforming document applies it, and nothing more.
