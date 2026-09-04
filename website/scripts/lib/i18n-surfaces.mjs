/**
 * The surfaces the Phase 5 render gate asserts on.
 *
 * WHY A REGISTRY AND NOT "every route". The gate renders each entry once per
 * locale, so cost is linear in this list. More importantly the ledger is keyed on
 * `id`, which makes a per-surface ceiling meaningful: adding untranslated copy to
 * `/schedule` raises `schedule`'s own number and fails, even if some other surface
 * improved in the same branch. A single global total would let that trade happen
 * silently — which is exactly the bidirectional-ratchet failure #1060 removed.
 *
 * SELECTION. Ranked by catalog text volume x traffic x label density. Every entry
 * is reachable by URL alone: the settings/capabilities/developer sub-panels are
 * query-param driven, so the gate needs no click choreography and cannot fail
 * because a selector moved. Modal and portal surfaces (command palette, credits
 * modal, composer dropdowns) are deliberately NOT here — they need interaction, and
 * a gate that flakes on a dropdown that did not open is a gate people disable.
 * `fixed-overlay-off-viewport` still covers the portals that mount on load.
 *
 * `settle` is extra idle time in ms for surfaces that fetch after first paint.
 */
/**
 * The app id `check-i18n-render.mjs`'s `FIXTURE_APPS[0]` is served under, and the
 * one the `app-detail` surface navigates to.
 *
 * It lives HERE, in the leaf module, rather than next to the fixture: the surface
 * URL and the fixture's `name` have to be the same string or the page renders
 * `app_not_found` — a real screen, so nothing crashes and nothing fails, it just
 * measures the wrong thing. Importing it the other way (fixture -> registry) would
 * be a cycle, since the script already imports `SURFACES` from here.
 */
export const FIXTURE_DETAIL_APP = 'fixture-research-lab'

export const SURFACES = [
  { id: 'chat', url: '/chat', settle: 400 },
  { id: 'settings-display', url: '/settings?tab=display' },
  { id: 'settings-overview', url: '/settings?tab=overview' },
  { id: 'schedule', url: '/schedule' },
  { id: 'settings-chat', url: '/settings?tab=chat' },
  { id: 'settings-privacy', url: '/settings?tab=privacy' },
  { id: 'settings-computer-use', url: '/settings?tab=computer-use' },
  { id: 'settings-security', url: '/settings?tab=security' },
  { id: 'settings-notifications', url: '/settings?tab=notifications' },
  { id: 'settings-about', url: '/settings?tab=about' },
  { id: 'capabilities-crews', url: '/capabilities?tab=crews' },
  { id: 'capabilities-mcp', url: '/capabilities?tab=mcp' },
  { id: 'capabilities-skills', url: '/capabilities?tab=skills' },
  { id: 'projects', url: '/projects' },
  { id: 'knowledge', url: '/knowledge' },
  // `settle: 400` is load-bearing, not padding. `SegmentedControl` animates its label
  // from `initial={{ width: 0 }}` to `animate={{ width: 'auto' }}` on a spring
  // (`stiffness: 500, damping: 35`, SegmentedControl.tsx:130-140), and the scanner
  // compares `scrollWidth` against `clientWidth`. Sample that mid-spring and the label
  // is still narrower than its settled width, so the ratio spikes past the expansion
  // budget and `layout/over-budget-truncation` fires on `[Ğàĺĺèŕý ···········]` — the
  // long-documented "nondeterministic artifacts.layout" flake, which is not noise but a
  // race against an animation. The default 250ms is not enough for this spring; the
  // other surfaces carrying post-paint motion already use 400.
  { id: 'artifacts', url: '/artifacts', settle: 400 },
  { id: 'apps', url: '/apps' },
  // The POPULATED app-detail body: description, the Features list, the tag chips,
  // the permission rows and the install/enable controls. `/apps` above only shows
  // cards; every one of those blocks lives here and none of it had been rendered.
  // The id must match `FIXTURE_DETAIL_APP` or the page renders `app_not_found`.
  { id: 'app-detail', url: `/apps/detail/${FIXTURE_DETAIL_APP}`, settle: 400 },
  { id: 'notifications', url: '/notifications' },
  { id: 'logs', url: '/logs' },
  { id: 'developer-system', url: '/developer?tab=system' },

  // BUILTIN APP SURFACES. Every entry below routes through the `/:builtinApp`
  // catch-all in `App.tsx`, resolved from `src/apps/builtinRegistry.ts`. None of
  // them was covered until now, and `/apps` above is the DISCOVER LIST, not any
  // app's own UI — so the whole of every builtin app rendered unscanned. The
  // static gate reported 482 findings in this code, and the classes only a render
  // can see (dynamic keys, backend-supplied English, concatenation seams,
  // overflow) had no coverage at all.
  //
  // `projects` above is the precedent: it is already a `/:builtinApp` route and
  // has rendered here since Phase 5 shipped, which is what makes this safe to
  // extend rather than a new mechanism.
  //
  // Ordered by measured static-finding density in the backing code, so the two
  // that dominate sit at the front and a `--surface` probe reaches them first.
  { id: 'design-critique', url: '/design-critique', settle: 400 },
  { id: 'issue-radar', url: '/issue-radar', settle: 400 },
  { id: 'dev-fleet', url: '/dev-fleet' },
  { id: 'auto-research', url: '/auto-research' },
  { id: 'channels', url: '/channels' },
  { id: 'file-explorer', url: '/file-explorer' },
  { id: 'workflows', url: '/workflows' },
  { id: 'code-review-sage', url: '/code-review-sage' },
  { id: 'papyrus', url: '/papyrus' },
  { id: 'crew-companion', url: '/crew-companion' },
  { id: 'meetings', url: '/meetings' },
  { id: 'md-notebook', url: '/md-notebook' },
  { id: 'worlds', url: '/worlds' },

  // THE REST OF THE QUERY-PARAM TABS. `settings`, `capabilities` and `developer`
  // each hold more tabs than were registered: 8 of 16 settings tabs, 3 of 7
  // capabilities tabs and 1 of 8 developer tabs. The unregistered ones are the
  // same shape as the registered ones — one URL, no click choreography — so they
  // were omitted by ranking, not by any property that makes them unscannable.
  // Leaving them out meant the gate's own selection criterion (text volume x
  // traffic x label density) was applied once and never revisited, while the
  // panels kept growing.
  { id: 'settings-browser', url: '/settings?tab=browser' },
  { id: 'settings-channels', url: '/settings?tab=channels' },
  { id: 'settings-developer', url: '/settings?tab=developer' },
  { id: 'settings-imports', url: '/settings?tab=imports' },
  { id: 'settings-instances', url: '/settings?tab=instances' },
  { id: 'settings-shortcuts', url: '/settings?tab=shortcuts' },
  { id: 'settings-skills', url: '/settings?tab=skills' },
  { id: 'settings-voice', url: '/settings?tab=voice' },
  { id: 'capabilities-hooks', url: '/capabilities?tab=hooks' },
  { id: 'capabilities-prompts', url: '/capabilities?tab=prompts' },
  { id: 'capabilities-steering', url: '/capabilities?tab=steering' },
  { id: 'capabilities-templates', url: '/capabilities?tab=templates' },
  { id: 'developer-archive', url: '/developer?tab=archive' },
  { id: 'developer-config', url: '/developer?tab=config' },
  { id: 'developer-logs', url: '/developer?tab=logs' },
  { id: 'developer-mcp-pool', url: '/developer?tab=mcp-pool' },
  { id: 'developer-memory', url: '/developer?tab=memory' },
  { id: 'developer-storage', url: '/developer?tab=storage' },
  { id: 'developer-telemetry', url: '/developer?tab=telemetry' },

  // Remaining parameter-free top-level routes.
  { id: 'hooks', url: '/hooks' },
  { id: 'deploy', url: '/deploy' },

  // STILL NOT COVERED, named rather than silently omitted:
  //
  //   - `/apps/:name` (the app HOST, `AppPage`) and `/apps/migrate/:name` — the
  //     host mounts the app's own remote ESM bundle, which no fixture can serve
  //     offline, so it would render its load-failure state rather than an app.
  //     `/apps/detail/:name` IS covered above, via `FIXTURE_DETAIL_APP`.
  //   - `/artifacts/:slug`, `/artifacts/remote/:provider/:id`,
  //     `/popout/artifact/:slug` — each needs a fixture that makes ONE id
  //     resolve, and an id that 404s renders a not-found panel instead of the
  //     surface, which would charge a number for the wrong screen.
  //   - `/embed/*`, `/popout/*`, `/worlds-popout` — a different shell with its
  //     own chrome, so they need their own boot fixtures before the
  //     `innerText > 100` liveness predicate can be trusted.
  //   - Modals, portals and dropdowns — unchanged from the note above: they need
  //     interaction. `fixed-overlay-off-viewport` still covers the portals that
  //     mount on load.
]

/**
 * Locales the gate renders.
 *
 * `en-XA` is the only one the text assertions run against — it is the locale where
 * un-accented Latin is provably untranslated — and it is now the only one measured
 * into the ledger at all. The single real locale beside it is a **DNT probe**: its
 * non-DNT findings are discarded, so it contributes nothing to `text`/`layout`/
 * `latent`, and it renders at one viewport because DNT is a content assertion that
 * does not depend on width.
 *
 * WHY IT SHRANK. Cost is `surfaces x locales x viewports`, doubled because
 * `[vs-base]` renders two trees. At 55 surfaces x 4 locales x 2 viewports that is
 * 880 page renders and ~23min, and this gate shares the `e2e` job's
 * `timeout-minutes: 25` with the Playwright suite — it blew the cap and got the whole
 * job CANCELLED before that suite ran even once. `en-XA` x 2 viewports plus one
 * real locale x 1 is 165 renders per tree, which fits with margin. The `en-XA` text
 * half is what widening the registry from 20 to 55 surfaces was FOR, so it is the
 * half that keeps full coverage.
 *
 * WHAT THAT COSTS, stated rather than discovered later:
 *
 *   - `de` (widest Latin, long compounds) used to grade the pseudolocale's synthetic
 *     padding against a real script, and `bn` (tallest line box) covered vertical
 *     growth. Without them the `layout` and `latent` buckets are measured ONLY under
 *     `en-XA`, whose padding is synthetic — so those two buckets now over-report
 *     relative to any shipped locale. That is a false-POSITIVE direction (a flagged
 *     site might fit in every real locale), not a missed defect, which is why it is
 *     the acceptable half to lose. Do not read `layout` as "clipped in a locale we
 *     ship" any more; read it as "clipped at pseudolocale width".
 *   - `ar` remains deliberately absent: no RTL locale ships until Track C.
 *
 * If layout grading in real locales is wanted back, the way in is a matrix that
 * shards surfaces across jobs — not adding locales back into one 25-minute job.
 *
 * DNT INTEGRITY IS NOT MEASURED HERE, and that is a deliberate move rather than a
 * gap. It cannot run under `en-XA` at all: `gen-pseudolocale.mjs` accents every
 * ASCII letter outside a preserved region and DNT terms are not preserved, so all 19
 * render mangled by design. With no real locale left in this list, keeping the
 * assertion here would mean a gate that renders, reports, exits 0 and checks no DNT
 * term — so it moved to `scripts/check-dnt-catalogs.mjs`, which compares catalog
 * VALUES instead of pixels. That is strictly wider (all 9 shipped locales, not the
 * one probe locale a render budget could afford) and costs no render time.
 */
export const LOCALES = [
  { code: 'en-XA', mode: 'pseudo' },
]

/**
 * Viewports. The narrow one is where fixed-width ancestors actually bite; the wide
 * one is the default desktop shell and the only place the topbar readout capsule and
 * the rail community row are laid out at all.
 */
export const VIEWPORTS = [
  { id: 'wide', width: 1440, height: 900 },
  { id: 'narrow', width: 1024, height: 768 },
]
