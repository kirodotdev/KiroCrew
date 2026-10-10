/**
 * Isolated capture entry for the v3 Dashboard tab: the states the read can
 * answer with, and the block patch applied.
 *
 * WHY THESE FRAMES: every one of them is a case a test asserts as an ABSENCE --
 * no page drawn, no mint, no held page -- and an absence is exactly what a
 * screenshot can falsify and an assertion cannot make legible. The `empty` row
 * carrying a composed default page is the regression this whole change exists
 * to prevent, and it is the one frame a reviewer should look at hardest: the
 * body has a full dashboard in it and the tab must show the empty state anyway.
 *
 * WHAT IS REAL: `CrewDynamicDashboard` is the real component, imported
 * unmodified, inside the real providers, with the real theme tokens and the
 * real i18n catalogs. Each row differs only in the BODY the read resolves.
 *
 * WHAT IS A STAND-IN, said plainly because a caption that overclaims is worse
 * than no caption:
 *
 *  - `api.memberDashboard` is stubbed per row. It is the controller's response,
 *    which the display line does not own.
 *  - `api.sandboxDocUrl` mints a `blob:` URL in the browser instead of calling
 *    the gateway. The document still lands on an opaque origin with
 *    `allow-scripts` and nothing else, which is the property the sandbox is
 *    about, so the frames are honest about containment.
 *  - the DOCUMENT inside the frame is a hand-written stand-in for the python
 *    renderer's output (the display line does not own that renderer either: D1
 *    does). It implements the renderer's TWO listeners: the full-paint one
 *    (`kirocrew-dashboard:data`, which replaces the read and re-initialises
 *    every block) and the block-patch one
 *    (`kirocrew-dashboard:block-patch`, which merges only the blocks it was
 *    given). It counts its own initialisations on `data-inits`, so the capture
 *    can assert a fold push did NOT take the full-paint path. That proves the
 *    HOST forwards the right object on the right type and a conforming document
 *    applies it. It proves nothing about the real renderer's internals, and no
 *    caption here claims it does.
 *
 * `?theme=dark` / `?theme=light`.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { api } from '../src/api/client'
import CrewDynamicDashboard from '../src/pages/members/CrewDynamicDashboard'
import { publishBlockPatch } from '../src/pages/members/dashboardBlockPush'
import { LanguageProvider } from '../src/i18n/LanguageProvider'
import { ThemeProvider } from '../src/hooks/useTheme'
import { store } from '../src/store'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const theme = new URLSearchParams(location.search).get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme)
document.documentElement.style.colorScheme = theme

/**
 * A stand-in for the renderer's document: the four block types a package may
 * place, and the same refill listener the real one has.
 *
 * Deliberately plain. It is not a proposal for how the renderer should look --
 * that is D1's -- it is the smallest document that (a) draws a recognisable
 * block of each type from a read, (b) beacons readiness, and (c) applies a
 * refill the host posts, so a patch frame has somewhere to land.
 */
function standInDocument(read: Record<string, unknown>): string {
  return `<!doctype html>
<html><head><meta charset="utf-8"><style>
  :root { color-scheme: ${theme}; }
  body { margin: 0; padding: 14px; font: 13px/1.5 ui-sans-serif, system-ui, sans-serif;
         background: var(--bg, ${theme === 'dark' ? '#16181d' : '#ffffff'});
         color: var(--fg, ${theme === 'dark' ? '#e6e8ec' : '#1b1d22'}); }
  .grid { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 10px; }
  .blk { border: 1px solid ${theme === 'dark' ? '#2c3038' : '#e3e5ea'}; border-radius: 8px; padding: 10px 12px; }
  .ttl { font-size: 10px; letter-spacing: .08em; text-transform: uppercase; opacity: .55; margin-bottom: 6px; }
  .stat { font-size: 30px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .row { display: flex; justify-content: space-between; gap: 10px; padding: 3px 0;
         border-bottom: 1px solid ${theme === 'dark' ? '#23262c' : '#f0f1f4'}; }
  .row:last-child { border-bottom: 0; }
  .k { opacity: .6; }
  .v { font-variant-numeric: tabular-nums; }
  .dim { opacity: .34; font-style: italic; }
  .agentic { font-size: 9px; padding: 1px 5px; border-radius: 99px; opacity: .8;
             border: 1px solid currentColor; margin-left: 6px; vertical-align: 1px; }
  .band { font-size: 11px; padding: 5px 8px; border-radius: 6px; margin-bottom: 10px;
          background: ${theme === 'dark' ? '#3a2d14' : '#fdf3dd'}; }
</style></head>
<body><div id="page"></div>
<script>
(function () {
  var read = ${JSON.stringify(read)};
  var BLOCKS = [
    { id: 'prs',     type: 'stat',     title: 'Open PRs',     fields: ['open_prs'] },
    { id: 'disk',    type: 'stat',     title: 'Disk',         fields: ['disk'] },
    { id: 'lanes',   type: 'table',    title: 'CI lanes',     fields: ['lanes_green', 'lanes_red'] },
    { id: 'notes',   type: 'list',     title: 'My call',      fields: ['my_call', 'blocked_on'] },
    { id: 'history', type: 'timeline', title: 'Last change',  fields: ['last_push'] }
  ];
  function shown(name) {
    if (read.display && Object.prototype.hasOwnProperty.call(read.display, name)) return read.display[name];
    if (read.fields && Object.prototype.hasOwnProperty.call(read.fields, name)) return String(read.fields[name]);
    return null;
  }
  function isAgentic(name) { return (read.agentic || []).indexOf(name) !== -1; }
  function cell(name) {
    var v = shown(name);
    if (v === null) return '<span class="dim">no value</span>';
    return '<span class="v">' + v + '</span>' + (isAgentic(name) ? '<span class="agentic">said so itself</span>' : '');
  }
  function fill() {
    var out = read.stale ? '<div class="band">Some values are older than this page.</div>' : '';
    out += '<div class="grid">';
    BLOCKS.forEach(function (b) {
      out += '<div class="blk" data-block="' + b.id + '"><div class="ttl">' + b.title + ' &middot; ' + b.type + '</div>';
      if (b.type === 'stat') {
        out += '<div class="stat">' + cell(b.fields[0]) + '</div>';
      } else {
        b.fields.forEach(function (f) {
          out += '<div class="row"><span class="k">' + f.replace(/_/g, ' ') + '</span>' + cell(f) + '</div>';
        });
      }
      out += '</div>';
    });
    out += '</div>';
    document.getElementById('page').innerHTML = out;
  }
  // TWO LISTENERS, because the renderer's document has two and the difference is
  // the whole point of this round. No backticks below: this whole document is a
  // TS template literal, and one closes it.
  //
  // The FULL-PAINT one replaces the read and re-initialises every block, which
  // is what the init counter records: a fold push arriving on this type would
  // run it again, and a block owning a canvas would get a second one with two
  // scenes animating over each other. The capture asserts the count stays 0.
  //
  // The BLOCK-PATCH one merges the blocks it was given and touches nothing else.
  var inits = 0;
  addEventListener('message', function (event) {
    if (event.source !== parent) return;
    var data = event.data;
    if (!data) return;
    if (data.type === 'kirocrew-dashboard:data') {
      if (!data.read || typeof data.read !== 'object') return;
      read = data.read;
      inits += 1;
      document.body.setAttribute('data-inits', String(inits));
      fill();
      return;
    }
    if (data.type === 'kirocrew-dashboard:block-patch') {
      // MERGE, never replace: only the blocks in the patch move, and each block
      // carries only its own fields. Nothing outside them is touched, and no
      // block is re-initialised.
      var blocks = data.blocks || {};
      Object.keys(blocks).forEach(function (id) {
        var entry = blocks[id] || {};
        Object.keys(entry.fields || {}).forEach(function (f) {
          read.fields = read.fields || {}; read.fields[f] = entry.fields[f];
        });
        Object.keys(entry.display || {}).forEach(function (f) {
          read.display = read.display || {}; read.display[f] = entry.display[f];
        });
      });
      if (typeof data.stale === 'boolean') read.stale = data.stale;
      if (Array.isArray(data.missing)) read.missing = data.missing;
      fill();
    }
  });
  document.body.setAttribute('data-inits', '0');
  fill();
  parent.postMessage({ type: 'kirocrew-dashboard:ready' }, '*');
})();
</script></body></html>`
}

/** Mint in the browser instead of asking the gateway. Same opaque origin. */
api.sandboxDocUrl = ((html: string) =>
  Promise.resolve({
    url: URL.createObjectURL(new Blob([html], { type: 'text/html' })),
  })) as typeof api.sandboxDocUrl

const LIVE_READ = {
  fields: {
    open_prs: 12,
    disk: 1200000000,
    lanes_green: 31,
    lanes_red: 2,
    my_call: 'holding #18438 until the rebase lands',
    last_push: '2026-10-10T00:51:00Z',
  },
  // THE RENDERER'S OWN FORMATTER. `1.2 GB` is here and not computed in the
  // browser, which is the property the reshape bought: one spelling of the
  // format rules, in the renderer.
  display: {
    open_prs: '12',
    disk: '1.2 GB',
    lanes_green: '31',
    lanes_red: '2',
    my_call: 'holding #18438 until the rebase lands',
    last_push: '10 Oct, 00:51',
  },
  agentic: ['my_call', 'blocked_on'],
  seq: 4821,
  stale: false,
  missing: ['blocked_on'],
  written_at: { my_call: '2026-10-10T00:44:00Z' },
  locale: 'en',
}

const PACKAGE_REF = {
  slug: 'oncall-dashboard',
  version: 7,
  layout_fingerprint: 'sha256:9f21c4',
  bound_to: 'crewmate:oncall',
}

/** Every row: the body the read resolves, and what the frame proves. */
const ROWS: { id: string; req: string; caption: string; body: unknown }[] = [
  {
    id: 'r1-live',
    req: 'R1 - the package read',
    caption:
      'state=live with a package descriptor (version 7) and the composed page. '
      + 'The frame carries data-layout-version=7, which is the number a patch is checked against. '
      + 'Document is a STAND-IN for the renderer (see the file header); the host, the read and the mint are real.',
    body: {
      state: 'live',
      package: PACKAGE_REF,
      push_version: 0,
      push_frame: 'dashboard_block_patch',
      blocks: {
        prs: ['open_prs'], disk: ['disk'], lanes: ['lanes_green', 'lanes_red'],
        notes: ['my_call', 'blocked_on'], history: ['last_push'],
      },
      missing: ['blocked_on'],
      rendered_html: standInDocument(LIVE_READ),
    },
  },
  {
    id: 'r2-empty',
    req: 'R2 - the empty state',
    caption:
      'state=empty and no page in the body: the crewmate has composed no dashboard. '
      + 'A STATE, not a failure - no error styling and deliberately NO retry button, '
      + 'because re-reading returns the same answer and what clears it is the crewmate composing a layout.',
    body: { state: 'empty', state_reason: 'no dashboard package', instance_version: 0 },
  },
  {
    id: 'r3-no-fallback',
    req: 'R3 - no default page',
    caption:
      'THE REGRESSION FRAME. state=empty but the body DOES carry a composed default template '
      + '(the route still sends one for a crewmate on the template registry). The tab shows the empty '
      + 'state anyway: nothing is minted and the default layout never reaches the DOM. '
      + 'Before this change, that default page was drawn in the box below, filled with this '
      + 'crewmate\'s own numbers and with nothing on it saying the layout was not theirs. '
      + 'Compare R1, which is the same box with a page the crewmate DID compose. '
      + 'PROVES THE FRONTEND HALF ONLY: the route is stubbed here, so this is the tab refusing '
      + 'a page it was sent - not the server declining to send one.',
    body: {
      state: 'empty',
      state_reason: 'no dashboard package',
      instance_version: 0,
      template: { id: 'project-report', version: 1 },
      rendered_html: standInDocument(LIVE_READ),
    },
  },
  {
    id: 'r3b-bound-no-fallback',
    req: 'R3b - no default page, for a PACKAGE-BOUND member',
    caption:
      'The same refusal on the shape the server-side scoping makes the dangerous one. '
      + 'The controller suppresses the builtin fallback only for a member whose dashboard comes '
      + 'from a PACKAGE, so THIS body - package bound_to this crewmate, state=empty, and a composed '
      + 'page anyway - is what the server would send again if that condition were widened or lost. '
      + 'PROVES THE FRONTEND HALF ONLY: the route is stubbed here, so this shows the tab refusing '
      + 'the page on its own authority. It does not show the server declining to send one.',
    body: {
      state: 'empty',
      state_reason: 'package bound but not composed',
      instance_version: 0,
      package: PACKAGE_REF,
      push_version: 0,
      push_frame: 'dashboard_block_patch',
      blocks: { prs: ['open_prs'] },
      missing: [],
      rendered_html: standInDocument(LIVE_READ),
    },
  },
  {
    id: 'r4-error',
    req: 'R4 - a dashboard that will not compose',
    caption:
      'state=error: the package is there and does not parse, so the controller composed nothing. '
      + 'Drawn as a failure WITH a retry, which is the difference from R2 - something is wrong here '
      + 'and trying again can change it.',
    body: { state: 'error', state_reason: 'package model invalid', instance_version: 5 },
  },
]

function Row({ row }: { row: (typeof ROWS)[number] }) {
  return (
    <section style={{ marginBottom: 26 }} data-capture-row={row.id}>
      <h2 style={{ font: '600 12px/1.4 ui-sans-serif, system-ui', letterSpacing: '.06em', textTransform: 'uppercase', opacity: 0.72, margin: '0 0 4px' }}>
        {row.req}
      </h2>
      <p style={{ font: '12px/1.5 ui-sans-serif, system-ui', opacity: 0.66, margin: '0 0 8px', maxWidth: 760 }}>
        {row.caption}
      </p>
      <div style={{ height: 300, border: '1px solid var(--border)', borderRadius: 10, overflow: 'hidden' }}>
        <CrewDynamicDashboard slug="oncall" member="oncall" displayName="On Call" />
      </div>
    </section>
  )
}

/**
 * The patch row, which is the only one that needs a control of its own: a
 * screenshot of an updated page proves nothing unless the same page was
 * photographed BEFORE the patch. So this row exposes a button the capture
 * script clicks between the two shots.
 */
function PatchRow() {
  return (
    <section data-capture-row="r5-patch">
      <h2 style={{ font: '600 12px/1.4 ui-sans-serif, system-ui', letterSpacing: '.06em', textTransform: 'uppercase', opacity: 0.72, margin: '0 0 4px' }}>
        R5 - the block patch, applied
      </h2>
      <p style={{ font: '12px/1.5 ui-sans-serif, system-ui', opacity: 0.66, margin: '0 0 8px', maxWidth: 760 }}>
        A fold advanced. The button publishes one real frame through the real
        router-to-tab bridge; the host checks it against the layout on screen and
        posts <code>read</code> into the document. Nothing in TypeScript formats a
        value: <code>disk</code> moves to <code>2.4 GB</code> because the frame says
        so. <code>blocked_on</code> stays dimmed - it is named in{' '}
        <code>missing</code>, and an empty string there would render as a filled
        cell holding nothing.
      </p>
      <button
        data-capture-action="publish-patch"
        style={{ font: '12px ui-sans-serif, system-ui', marginBottom: 8, padding: '4px 10px' }}
        onClick={() =>
          publishBlockPatch({
            slug: 'oncall',
            dashboard: 'oncall-dashboard',
            version: 1,
            layout: 7,
            fold: 'work',
            blocks: { prs: ['open_prs'], disk: ['disk'], lanes: ['lanes_green', 'lanes_red'] },
            missing: ['blocked_on'],
            // The RENDERER's payload, forwarded verbatim: per-block, already
            // formatted, carrying its own type. Only the three blocks that read
            // the advancing fold are in it, which is why the timeline and the
            // notes block below keep the values they were first painted with.
            patch: {
              type: 'kirocrew-dashboard:block-patch',
              blocks: {
                prs: { fields: { open_prs: 31 }, display: { open_prs: '31' } },
                disk: { fields: { disk: 2400000000 }, display: { disk: '2.4 GB' } },
                lanes: {
                  fields: { lanes_green: 31, lanes_red: 0 },
                  display: { lanes_green: '31', lanes_red: '0' },
                },
              },
              seq: 4903,
              stale: false,
              missing: ['blocked_on'],
            },
            refetch: false,
            reason: '',
          })
        }
      >
        publish one block patch
      </button>
      <div style={{ height: 300, border: '1px solid var(--border)', borderRadius: 10, overflow: 'hidden' }}>
        <CrewDynamicDashboard slug="oncall" member="oncall" displayName="On Call" />
      </div>
    </section>
  )
}

/** The patch row's own body, named so the module-scope stub can reach it. */
const PATCH_BODY = {
  state: 'live',
  package: PACKAGE_REF,
  push_version: 0,
  push_frame: 'dashboard_block_patch',
  blocks: {
    prs: ['open_prs'], disk: ['disk'], lanes: ['lanes_green', 'lanes_red'],
    notes: ['my_call', 'blocked_on'], history: ['last_push'],
  },
  missing: ['blocked_on'],
  rendered_html: standInDocument(LIVE_READ),
}

/**
 * ONE ROW PER PAGE LOAD, chosen by `?row=`, and the stub assigned at MODULE
 * SCOPE rather than in an effect.
 *
 * Not a style choice. `CrewDynamicDashboard` fires its read on mount, so a stub
 * installed in a parent's `useEffect` arrives after the child has already
 * called the REAL `api.memberDashboard` -- which in a capture page reaches no
 * gateway, so every row rendered the read-failed state and the first run wrote
 * no frames at all. Deciding the row before React starts is what makes each
 * frame show the state its caption claims.
 */
const ONLY = new URLSearchParams(location.search).get('row') ?? 'r1-live'
const SELECTED = ROWS.find(r => r.id === ONLY)
const BODY = SELECTED ? SELECTED.body : PATCH_BODY
api.memberDashboard = (() => Promise.resolve(BODY)) as typeof api.memberDashboard

/**
 * ONE client for the page, and the SAME provider stack the component has in the
 * app: query client, Redux store, theme, language.
 *
 * All four are load-bearing and each was found by the page rendering nothing at
 * all: `LanguageProvider` runs a query to resolve the reader's language, and
 * `useTheme` throws outside `ThemeProvider`. A capture that renders an empty
 * body is the one failure mode a screenshot cannot report on itself, which is
 * why `shoot.py` asserts a testid per frame before it writes a file.
 */
const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })

function App() {
  return (
    <QueryClientProvider client={client}>
      <Provider store={store}>
        <ThemeProvider>
          <LanguageProvider>
            <div style={{ padding: 18, maxWidth: 860 }}>
              {SELECTED ? <Row row={SELECTED} /> : <PatchRow />}
            </div>
          </LanguageProvider>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
}

// Synchronous, like every sibling capture entry: the catalogs are statically
// imported by `i18n/all`, so there is nothing to await. The empty state's
// sentence IS a catalog string, and an uninitialised i18n would render it blank
// -- which is exactly the pixel the R2 and R3 frames exist to show.
initI18n('en')
createRoot(document.getElementById('root')!).render(<App />)
