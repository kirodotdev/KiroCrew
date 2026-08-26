#!/usr/bin/env node
/**
 * Phase 5 — the render-time i18n gate.
 *
 * ## What this catches that nothing else can
 *
 * Every other i18n gate in this repo reads source or catalog JSON:
 *   - `check-i18n-strings.mjs` runs eslint over `src` — it sees a string literal.
 *   - `check-source-strings.mjs` / `qa.test.ts` read catalog values.
 *   - `catalogParity` / `deadKeys` / `glossary` compare catalogs to each other.
 *
 * None of them can see the rendered page, and three whole defect classes live only
 * there. A hardcoded `"6m 38s"` built by `useUptime.ts` is ordinary TypeScript to a
 * source scanner. A sentence assembled from three catalog keys is three correct
 * lookups. An English `aria-label` never enters the text flow at all. This gate
 * renders the real built SPA under the `en-XA` pseudolocale and reads them off the
 * DOM, which is why the plan calls it "the rendered proof of Phases 1-3, not a
 * catalog assertion".
 *
 * ## How it runs with no gateway and no credentials
 *
 * It serves the REAL build over loopback (`lib/serve-dist.mjs`) and answers every
 * `/api/**` call from fixtures (`lib/stub-dashboard-api.mjs`), the same mechanism
 * the `capture-*.mjs` harnesses use. No token, no Python backend, no model calls.
 *
 * ## The one build requirement
 *
 * `en-XA` is DEV-only in three independent places (`index.ts` tree-shakes the
 * catalog out, `isRestorableLanguage()` refuses the stored code, `DETECTABLE_CODES`
 * hides it), all keyed on `import.meta.env.DEV`. `vite build --mode development` is
 * NOT enough — Vite derives DEV from `NODE_ENV`, not from `--mode` — so the bundle
 * this gate needs is built with `NODE_ENV=development`. Verified: without it the
 * accented catalog is absent from `dist/assets/*.js` entirely and every surface
 * renders English, which would make the gate silently pass. `assertPseudoActive()`
 * below exists so that failure mode can never be silent again.
 *
 * ## Enforcement shape (post-#1060 doctrine)
 *
 *   - `dnt` findings are ZERO TOLERANCE. The catalog-level DNT check
 *     (`glossary.test.ts`) already passes, so any render-level violation is new.
 *   - **[vs-base] is the ONLY thing that fails.** With `I18N_BASE_REF` set, this
 *     renders the BASE commit too — same scanner, same surfaces, same locales, only
 *     the bundle differs — and fails on any per-surface increase. It reads no
 *     committed number, so there is nothing to re-snapshot and nothing to absorb a
 *     regression with. Every path to `main` supplies a base: a PR is diffed against
 *     its base branch, and a push to `main` against the commit it replaced
 *     (`ci.yml` passes `github.event.before`).
 *   - the LEDGER (`src/i18n/render-baseline.json`) is a REPORT. It is printed in
 *     both directions and never fails a run. A total cannot be attributed to a diff:
 *     #1107 recorded `projects.latent: 16` measured on its own base while #985
 *     independently added one `min-w-0`-less `truncate` to the Projects rail, and
 *     after both merged `main` measured 24 against 16 and went red on a defect that
 *     neither branch's diff introduced. See `lib/render-verdict.mjs`.
 *
 * A finer ledger would not have helped, and it is worth being precise about why:
 * per-surface keys only shrink the range a regression can hide within. Measured —
 * inflate `settings-about.text` from 28 to 100, add 18 real defects, and the ledger
 * reports an *improvement* while [vs-base] fails with `28 -> 46 (+18)`.
 *
 * Usage:
 *   npm run i18n:render                                   # build + gate (+ vs-base)
 *   node scripts/check-i18n-render.mjs --build --update    # build + rewrite the debt record
 *   node scripts/check-i18n-render.mjs                     # gate an existing dist-dev
 *   node scripts/check-i18n-render.mjs --no-vs-base        # report only, skip base render
 *   node scripts/check-i18n-render.mjs --surface chat --locale en-XA --verbose
 *   I18N_BASE_REF=origin/main node scripts/check-i18n-render.mjs --build
 *
 * Exit codes: 0 clean · 1 regression · 2 cannot run (build missing, no browser,
 * pseudolocale not active).
 */
import { readFileSync, writeFileSync, existsSync, rmSync, mkdirSync, symlinkSync } from 'node:fs'
import { spawnSync, execFileSync } from 'node:child_process'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems, json } from './lib/stub-dashboard-api.mjs'
import { SURFACES, LOCALES, VIEWPORTS, FIXTURE_DETAIL_APP } from './lib/i18n-surfaces.mjs'
import { browserBundle } from './lib/render-scan.mjs'
import {
  BUCKETS,
  BUCKET_MEANING,
  LEDGER_COMMENT,
  addedFindings,
  bucketOf,
  decide,
  fmtDelta,
  identityIsExact,
  tally,
  totals,
} from './lib/render-verdict.mjs'

const SCAN_SRC = fileURLToPath(new URL('./lib/render-scan.mjs', import.meta.url))
const LEDGER = fileURLToPath(new URL('../src/i18n/render-baseline.json', import.meta.url))
const GLOSSARY = fileURLToPath(new URL('../src/i18n/glossary.json', import.meta.url))
/**
 * ONE output stream, always.
 *
 * Everything this script prints goes through here, and it is stdout for failures too.
 * Mixing `console.log` with `console.error` shredded the log: Node buffers stdout and
 * stderr independently once they are pipes rather than a TTY, GitHub Actions merges
 * both into one stream, and the two blocks interleaved line-by-line —
 *
 *     chat.latent: 48 -> 64 (+16)
 *         282  latin-leak/untranslated-attribute
 *     settings-display.latent: 16 -> 32 (+16)
 *            worst: en-XA
 *
 * — which is unreadable and, worse, looks like the gate reporting nonsense. The
 * failure signal is the EXIT CODE; it never needed a separate stream.
 */
const out = line => process.stdout.write(`${line}\n`)

/** A GitHub Actions collapsed section; plain lines anywhere else. */
const group = (title, lines) => {
  if (!lines.length) return
  const ci = !!process.env.GITHUB_ACTIONS
  out(ci ? `::group::${title}` : `\n${title}`)
  for (const l of lines) out(l)
  if (ci) out('::endgroup::')
}

/** One finding, with the source location that says which line to edit. */
const fmtFinding = (rec, indent = '    ', count = 1, details = []) => {
  const { surface, locale, finding } = rec
  const lines = [
    `${indent}${finding.kind}/${finding.signature}  [${surface} · ${locale}]${count > 1 ? `  ×${count}` : ''}`,
    `${indent}  ${JSON.stringify(finding.text)}`,
  ]
  if (finding.source && finding.source.length) {
    // Leaf first — for an inline string that IS the line. The `via` hops matter when a
    // shared component rendered a string its CALLER hardcoded; then the answer is the
    // first `via`. See `sourceChain` in lib/render-scan.mjs for the measurement.
    lines.push(`${indent}  ${finding.source[0]}`)
    for (const via of finding.source.slice(1)) lines.push(`${indent}  via ${via}`)
  } else {
    // Never silently degrade to the DOM path — say the mapping was unavailable, or
    // the next React upgrade removes `_debugSource` and nobody notices for months.
    lines.push(`${indent}  no source location (React _debugSource unavailable) — DOM: ${finding.path}`)
  }
  // More than one run leaked from the same element: the templated `fix` quotes only
  // the first, so list them all rather than letting the author fix one of three.
  if (details.length > 1) {
    lines.push(`${indent}  untranslated runs: ${details.map(d => JSON.stringify(d)).join(', ')}`)
  }
  if (finding.fix) lines.push(`${indent}  ${finding.fix}`)
  return lines
}

/**
 * Collapse findings that are the same defect seen more than once.
 *
 * One hardcoded `"Beta preview"` produced SIX findings: the scanner reports each
 * untranslated RUN separately ("Beta", then "preview"), the element and its parent both
 * carry the text, and every surface is swept at two viewports. Reporting 26 rows for
 * three mistakes is how a correct list becomes an ignored one.
 *
 * Grouped by SITE — signature + source location + text — deliberately excluding
 * `detail`, which is "which run leaked" and is what split one `title` attribute into
 * three rows. The distinct runs are merged onto one line instead, so the row still says
 * which words to extract.
 *
 * The COUNTS are untouched: `identityOf` keeps `detail`, and `addedFindings` stays a
 * multiset, because sixteen findings from one `.map()` are sixteen findings. This
 * collapses only what is printed.
 */
const groupForDisplay = records => {
  const groups = new Map()
  for (const rec of records) {
    const { finding } = rec
    const key = [
      finding.signature,
      (finding.source || []).join('<') || `path:${finding.path}`,
      finding.text || '',
    ].join('|')
    const prev = groups.get(key)
    if (prev) {
      prev.count++
      if (finding.detail && !prev.details.includes(finding.detail)) prev.details.push(finding.detail)
    } else {
      groups.set(key, { rec, count: 1, details: finding.detail ? [finding.detail] : [] })
    }
  }
  return [...groups.values()]
}

/** Repo root — `website/`'s parent, where every git call is rooted. */
const REPO = fileURLToPath(new URL('../..', import.meta.url))

/**
 * This checkout's `website/node_modules`. The base bundle is built against it, so it
 * is also what decides whether a base tree is buildable at all.
 */
const NODE_MODULES = join(fileURLToPath(new URL('..', import.meta.url)), 'node_modules')

const argv = process.argv.slice(2)
const has = f => argv.includes(f)
const opt = (f, d) => {
  const i = argv.indexOf(f)
  return i >= 0 && argv[i + 1] ? argv[i + 1] : d
}

const BUILD = has('--build')
const NO_VS_BASE = has('--no-vs-base')
const UPDATE = has('--update')
const VERBOSE = has('--verbose')
/** Why the diff half did not run, so the verdict line can name it. */
let SKIP_REASON = ''
const DIST = fileURLToPath(new URL(`../${opt('--dist', 'dist-dev')}/`, import.meta.url))
const ONLY_SURFACE = opt('--surface', '')
const ONLY_LOCALE = opt('--locale', '')

const die = msg => {
  out(`\n[i18n-render] ${msg}\n`)
  process.exit(2)
}

/**
 * Fixture values are deliberately digit-shaped or non-Latin.
 *
 * Anything word-shaped in a fixture renders into the page and is indistinguishable
 * from a hardcoded English string, so it would show up as a leak the branch cannot
 * fix. The shared stub's defaults (`user: 'owner'`, agents `kirocrew`/`oncall`) are
 * overridden below for exactly that reason.
 *
 * The two non-obvious shapes are load-bearing, and both error-boundary the WHOLE
 * app shell rather than degrading:
 *   - a chat slot with no `key`: the command palette's recents provider maps
 *     `slots.map(s => s.key.startsWith('dashboard_'))`, so a keyless slot throws on
 *     every route that mounts the palette — which is all of them.
 *   - `/api/security/denied-commands` as an array: SecurityPanel guards `dc?.builtins`
 *     in one place but reads `dc.builtins.length` unguarded in another, and `[]` is
 *     truthy, so the panel throws instead of rendering empty.
 */
const SLOT_KEY = '0100'

const FIXTURE_SLOTS = [{
  key: SLOT_KEY,
  title: '0101',
  running: false,
  last_message: '0102',
  messages: 2,
  agent: '0001',
  memory_mode: 'persistent',
  project: '/0002',
  model: '',
  reasoning_effort: '',
  modified: 1750000000,
  source_links: [],
  source_links_total: 0,
}]

/**
 * Installed-app records for `GET /api/apps` and `GET /api/apps/<name>`.
 *
 * `origin: 'builtin'` is load-bearing: `AppsPage.browseApps` builds the Discover
 * catalogue from `apps.filter(a => a.origin === 'builtin' && !a.manifest?.hidden)`,
 * so any other origin renders the same card-less storefront this fixture exists to
 * replace. Two entries rather than one because `pickFeatured` consumes the first for
 * the spotlight and the card grid only starts at the second.
 *
 * Which fields carry LATIN and which carry a digit placeholder is a deliberate
 * split, not an inconsistency:
 *
 *   - `displayName` / `description` / `highlights[]` / `tags[]` are Latin. They
 *     render ONLY on `/apps` and `/apps/detail/:name`, and they are exactly the
 *     backend-owned values the components interpolate raw. Bare Latin at those
 *     nodes under en-XA is the finding.
 *   - `ui.pages[].label` is a digit placeholder. `App.tsx`'s nav rail resolves
 *     `page.label || a.displayName || a.name` for every installed app, so the rail
 *     is GLOBAL chrome: Latin there would land the same finding on all 50+ surfaces
 *     and drown the two that are about app metadata. The placeholder still makes
 *     the rail's Apps group render (it has been empty on every surface until now,
 *     because `/api/apps` answered `[]`), which is the part worth covering here.
 *   - `permissions` carries booleans only. The permission ROWS are product copy and
 *     should be scanned; an `api: ['/api/chat/*']` entry would add a fixture-owned
 *     path string to the count without adding any product signal.
 */
const FIXTURE_APPS = [
  {
    name: FIXTURE_DETAIL_APP,
    version: '1.0.0',
    displayName: 'Fixture Research Lab',
    enabled: true,
    installedAt: '2026-01-01T00:00:00Z',
    origin: 'builtin',
    resources: 'gateway',
    lifecycle: 'locked',
    manifest: {
      name: FIXTURE_DETAIL_APP,
      version: '1.0.0',
      displayName: 'Fixture Research Lab',
      description: 'Runs research campaigns unattended.',
      author: '0008',
      tags: ['research', 'automation'],
      highlights: [
        'Expands questions into subtrees.',
        'Cites every finding.',
      ],
      ui: { pages: [{ route: '/fixture-research-lab', label: '0009', icon: 'FlaskConical' }] },
      permissions: { storage: true, cron: true, network: true },
    },
  },
  {
    name: 'fixture-issue-radar',
    version: '2.1.0',
    displayName: 'Fixture Issue Radar',
    enabled: false,
    installedAt: '2026-01-02T00:00:00Z',
    origin: 'builtin',
    resources: 'gateway',
    lifecycle: 'locked',
    manifest: {
      name: 'fixture-issue-radar',
      version: '2.1.0',
      displayName: 'Fixture Issue Radar',
      description: 'Triages incoming issue reports.',
      author: '0008',
      tags: ['developer-tools'],
      highlights: [
        'Groups reports by component.',
        'Leaves triage evidence.',
      ],
      ui: { pages: [{ route: '/fixture-issue-radar', label: '0010', icon: 'Radar' }] },
      permissions: { storage: true, network: true },
    },
  },
]

const FIXTURE_OVERRIDES = async (language, path, route) => {
  // `stubDashboardApi` treats a TRUTHY return as "handled". `json()` resolves to
  // undefined (that is what `route.fulfill()` returns), so an override must await
  // it and return true explicitly — returning the promise falls through to the
  // shared arm and Playwright throws "Route is already handled".
  const done = async body => { await json(route, body); return true }
  // LanguageProvider treats the boot payload as authoritative over the
  // localStorage fast-path, so a payload with no `language` reverts the UI to
  // English mid-boot and turns a localised pass into an English one.
  if (path === '/api/theme/boot') return done({ mode: 'dark', theme: '', language })
  if (path === '/api/auth/me') return done({ user: '0000', app: '' })
  if (path === '/api/agents' || path === '/api/chat/agents') {
    return done([{ name: '0001', source: 'builtin' }])
  }
  if (path === '/api/recent-projects') return done({ dirs: ['/0002'] })
  if (path === '/api/dashboard/branding') return done({ bot_name: 'Kiro', avatar: '' })
  if (path === '/api/chat/slots') return done(FIXTURE_SLOTS)
  if (path.startsWith('/api/chat/slots/')) {
    return done({ key: SLOT_KEY, messages: [], running: false, agent: '0001' })
  }
  if (path === '/api/security/denied-commands') {
    return done({ builtins: [], user_added: [], policy_pinned: [] })
  }
  if (path === '/api/security/posture') return done({ posture: {} })
  // BUILTIN APP COLLECTION SHAPES. `handleBootRoute`'s fallback answers an
  // unknown path with `[]` (or `{}` for a config-ish name), and for most app
  // endpoints that is fine — the panel renders its empty state, which is exactly
  // the copy we want scanned. These two crash on it instead, and a crash is not a
  // finding: React's error boundary swaps the panel for its own English message,
  // the scan counts that message, and the surface's number stops describing the
  // product. `check-i18n-render.mjs` turns any page error into exit 2 for that
  // reason, so a new app surface that dies here needs its shape declared, not the
  // surface dropped.
  //
  // file-explorer: `FileExplorerPage` guards with `if (treeData?.entries)` and
  // then calls `.forEach`. Against the `[]` fallback that guard PASSES —
  // `[].entries` is `Array.prototype.entries`, a truthy function — and the
  // `.forEach` on it throws. The guard is only sound against an object.
  if (path.startsWith('/apps/file-explorer/api/')) {
    if (path.startsWith('/apps/file-explorer/api/health')) {
      return done({ allowedRoots: ['/0003'] })
    }
    if (path.startsWith('/apps/file-explorer/api/search')) return done({ results: [] })
    if (path.startsWith('/apps/file-explorer/api/git-status')) return done(null)
    return done({ entries: [] })
  }
  // code-review-sage: `settings.models.map` / `nsData.namespaces.map` read arrays
  // straight off the response with no optional chaining.
  if (path.startsWith('/api/apps/code-review-sage/settings')) {
    return done({ settings: {}, models: [], efforts: [], namespaces: [] })
  }
  if (path.startsWith('/api/apps/code-review-sage/namespaces')) {
    return done({ namespaces: [], active: [] })
  }
  if (path.startsWith('/api/apps/code-review-sage/runs')) return done({ runs: [] })
  // Two more panels read nested collections off a config payload with no optional
  // chaining, so the `{}` an objectish path falls back to is not enough:
  // `KiroCrewCfgTab` does `Object.entries(cfg.agents)` and `VoicePanel`'s provider
  // row does `Object.keys` on its own config. Same class as the two apps above.
  if (path === '/api/config/kirocrew') {
    return done({
      agents: {}, workspaces: {}, memory_stores: {}, session: {}, memory: {},
      agent: {}, auto_update: {},
      default_agent: '', default_workspace: '', default_memory_store: '',
    })
  }
  if (path === '/api/voice/config') {
    return done({
      // `provider` must be one of `PROVIDER_OPTIONS`; anything else and the panel
      // renders neither provider's rows, which is a different screen.
      enabled: false, provider: 'piper', voice: '0004', engine: '0005', rate: '0006',
      autoSpeak: false, aws_profile: '', region: '',
      piper_binary: '', piper_model: '', piper_model_config: '', piper_length_scale: 1,
    })
  }
  if (path === '/api/voice/voices') return done({ voices: [] })
  // `SttSettings` (mounted inside VoicePanel) does `Object.keys(stt.models)` with
  // no guard, and `/api/config/stt` falls back to `{}` for holding `config`.
  if (path === '/api/config/stt') {
    return done({
      enabled: false, provider: '0007', model: '', mlx_model: '',
      available: false, models: {}, mlx_models: {},
    })
  }
  // APP STORE + APP DETAIL. `handleBootRoute`'s fallback answers `/api/apps` and
  // `/api/apps/registry` with `[]`, so both render their EMPTY state: the Discover
  // grid draws zero cards and `/apps/detail/:name` draws `app_not_found`. Neither
  // is a crash, so nothing failed -- the surfaces just silently described the wrong
  // screen. `apps` recorded 2 text findings for a storefront that has a spotlight,
  // category chips, card rows, an author line and a tag list.
  //
  // The prose below is deliberately LATIN, not the digit placeholders the fixtures
  // above use, and that difference is the point of this fixture. Every field here
  // is a field the Python side owns (`apps/builtins/*/app.json` ->
  // `discovery.py` -> `GET /api/apps`), and the components render it straight:
  // `{app.displayName}`, `{app.description}`, `highlights.map`. Under en-XA a
  // catalog lookup comes back bracketed, so bare Latin at those nodes is proof the
  // value never passed through one. A digit placeholder would render the same
  // surface while hiding exactly the defect this surface exists to show.
  //
  // Read the resulting numbers as a FIXED STRUCTURAL PROBE, not as the size of the
  // debt: the scanner counts per word, so the count is whatever prose this fixture
  // carries -- which is why the strings below are terse rather than realistic
  // marketing copy. They are long enough to prove the bypass and short enough not
  // to dominate the surface's entry in the record. The real population is 128
  // manifest strings across 14 builtins, and its ground truth is a server-side
  // pseudolocale, the same way `test_error_code_contract.py` declares it for
  // backend error bodies. Tracked as D14-a on #1004.
  if (path === '/api/apps') return done(FIXTURE_APPS)
  if (path === '/api/apps/registry') {
    return done({ apps: [], serverPlatform: { os: 'linux', arch: 'x64' } })
  }
  if (path === '/api/apps/registries') return done({ registries: [] })
  // Exact ids, not a `/api/apps/` prefix: a prefix arm here would shadow the
  // per-app sub-paths declared above (`/api/apps/code-review-sage/settings`).
  for (const app of FIXTURE_APPS) {
    if (path === `/api/apps/${app.name}`) return done(app)
  }
  if (path === '/api/system') return done({ hostname: '0008', os: 'linux', arch: 'x64' })
  return false
}

/**
 * Run a DEV-mode vite build and return only whether it worked.
 *
 * Output is CAPTURED, not inherited: vite prints a line per emitted asset, and this
 * gate runs two builds, so inheriting would bury the actual findings under ~200 lines
 * of asset table in the CI log. On failure the tail is printed, which is the only time
 * any of it is useful.
 */
function runViteDevBuild(cwd, outDir, label) {
  const vite = fileURLToPath(new URL('../node_modules/vite/bin/vite.js', import.meta.url))
  if (!existsSync(vite)) die(`cannot find vite at ${vite}; run \`npm ci\` in website/ first.`)
  const started = Date.now()
  const res = spawnSync(
    process.execPath,
    [vite, 'build', '--mode', 'development', '--outDir', outDir, '--emptyOutDir'],
    {
      cwd,
      // The ONLY thing that makes `import.meta.env.DEV` true in a build. `--mode
      // development` alone is not enough: Vite derives DEV from NODE_ENV, and with
      // just the mode flag the en-XA catalog is tree-shaken out and every surface
      // renders English.
      env: { ...process.env, NODE_ENV: 'development' },
      encoding: 'utf-8',
    },
  )
  if (res.status !== 0) {
    out(`${res.stdout || ''}\n${res.stderr || ''}`.trim().split('\n').slice(-25).join('\n'))
    die(`[${label}] vite build failed (exit ${res.status})`)
  }
  out(`[i18n-render] [${label}] built in ${((Date.now() - started) / 1000).toFixed(0)}s`)
}

/**
 * Build the bundle this gate needs, in-process.
 *
 * This lives here rather than in an npm script because the requirement is a
 * NODE_ENV, and `"NODE_ENV=development vite build"` in package.json is shell
 * syntax that cmd.exe does not understand — no other script in this file needs a
 * shell, and adding `cross-env` for one line is not worth a dependency. Spawning
 * vite's JS entry through `process.execPath` also avoids the `.bin` shim, which is
 * the same Windows ENOENT trap `check-source-strings.mjs` hit with `npx`.
 */
function buildDevBundle(outDir) {
  runViteDevBuild(fileURLToPath(new URL('..', import.meta.url)), outDir, 'HEAD')
}

/**
 * Decide whether to run the diff-scoped half, and against which commit.
 *
 * Mirrors `check-i18n-strings.mjs`'s contract deliberately, including the parts that
 * look defensive:
 *   - `I18N_BASE_REF` accepts a ref name OR a bare SHA, because the two contexts that
 *     supply it differ: a PR passes `origin/<base branch>`, while a push to `main`
 *     passes `github.event.before` — the commit the push replaced. Both are a real
 *     diff, and the push one is the only place an interaction between two branches
 *     that never touched the same file can be attributed to the merge that caused it.
 *   - no `I18N_BASE_REF` at all means nothing is enforced this run, which
 *     `reconcile()` announces rather than exiting 0 as though it had checked.
 *   - a ref that IS configured but cannot be resolved is a HARD failure, never a
 *     green skip. `ci.yml` records having watched a sibling gate skip itself green on
 *     a failed fetch and silently stop checking for a whole PR.
 *   - prefer the merge base so commits that landed on the base branch after this one
 *     forked are not attributed to this branch; fall back to the base tip, because CI
 *     checks out at depth 1 and fetches the base at depth 1 too, so the two have no
 *     shared history and `merge-base` fails there.
 */
function resolveBaseScope() {
  if (NO_VS_BASE) return { run: false, reason: '--no-vs-base' }
  if (ONLY_SURFACE || ONLY_LOCALE) return { run: false, reason: 'partial run (--surface/--locale)' }
  if (UPDATE) return { run: false, reason: '--update only rewrites the debt record' }
  const baseRef = process.env.I18N_BASE_REF
  if (!baseRef) {
    // `fallback` separates "no diff was AVAILABLE" from the three opt-outs above, which
    // asked for a report and must keep getting one. With no base there is nothing left
    // to check, so the debt record stops being a report and becomes the guard — see
    // `decide()`'s `totalIsFallback`.
    return {
      run: false,
      fallback: true,
      reason: 'I18N_BASE_REF is unset, so there is no commit to diff against',
    }
  }

  const git = args => execFileSync('git', args, { cwd: REPO, encoding: 'utf-8' }).trim()
  try {
    git(['rev-parse', '--verify', `${baseRef}^{commit}`])
  } catch {
    die(`cannot resolve ${baseRef}. The diff-scoped render gate needs the base commit; fetch it\n`
      + '    before running this script, or unset I18N_BASE_REF to report counts without a gate.')
  }

  let sha
  try {
    sha = git(['merge-base', baseRef, 'HEAD'])
  } catch {
    sha = git(['rev-parse', `${baseRef}^{commit}`])
  }
  if (sha === git(['rev-parse', 'HEAD'])) {
    return { run: false, reason: 'HEAD is the base commit' }
  }

  // Nothing that can move a number changed, so the base render is guaranteed identical
  // and the second build+sweep would be ~2.5 minutes of CI for a known answer.
  //
  // This filter is load-bearing now that the debt record no longer fails: a path that
  // escapes it gets NO check at all, where before it still had to clear the ledger. So
  // it covers everything that can move a count, not just what renders:
  //   - `src/`                app source and the catalogs
  //   - `scripts/`            the scanner (`lib/render-scan.mjs`), the surface list
  //                           (`lib/i18n-surfaces.mjs`) and the API fixtures
  //                           (`lib/stub-dashboard-api.mjs`), whose text is rendered
  //                           and scanned like any other string
  //   - `public/`             assets served into the rendered page
  //   - build config          `index.html`, `vite.config*`, `tailwind*`, `postcss*`,
  //                           `tsconfig*`, and `package.json` / `package-lock.json`
  //                           (bare `package` matches both — a dependency bump can
  //                           change what renders)
  // Adding a surface to `i18n-surfaces.mjs` is the case that decided this: its numbers
  // start at 0 and every finding it brings is growth, which must face a diff.
  const changed = git(['diff', '--name-only', `${sha}`, 'HEAD']).split('\n').filter(Boolean)
  const renderable = changed.filter(
    f => /^website\/(src\/|scripts\/|public\/|index\.html|vite\.config|tailwind|postcss|tsconfig|package)/.test(f),
  )
  if (!renderable.length) {
    return { run: false, reason: `no renderable file changed vs ${sha.slice(0, 8)} (${changed.length} file(s) in diff)` }
  }

  // The base bundle is built against HEAD's `node_modules` (see `buildBaseBundle`),
  // so a branch that REMOVES a frontend dependency the base still imports leaves the
  // base tree unbuildable — and the failure lands on code that is not in the diff.
  // Skip rather than die: policing the base's dependency set is explicitly not this
  // gate's job, and exiting 2 here fails a branch for tidying a dependency up.
  //
  // Read from the base commit instead of the exported tree, so this costs one
  // `git show` rather than an archive that is about to be thrown away.
  let baseDeps = {}
  try {
    const basePkg = JSON.parse(git(['show', `${sha}:website/package.json`]))
    baseDeps = { ...(basePkg.dependencies || {}), ...(basePkg.devDependencies || {}) }
  } catch {
    // A base with no readable manifest is the pre-existing `buildBaseBundle` failure
    // mode, not this one; leave it to report there.
  }
  const missing = Object.keys(baseDeps).filter(
    name => !existsSync(join(NODE_MODULES, ...name.split('/'), 'package.json')),
  )
  if (missing.length) {
    return {
      run: false,
      fallback: true,
      reason: `base tree ${sha.slice(0, 8)} needs ${missing.join(', ')}, which this branch's `
        + 'lockfile removes, and the base bundle builds against this branch\'s node_modules',
    }
  }
  return { run: true, sha, changed: renderable.length }
}

/**
 * Build the base commit's bundle from a read-only export of its tree.
 *
 * `git archive` rather than `git worktree add`, deliberately and after being burned:
 * a worktree REGISTERS itself in the repo, so it needs cleanup, and a run killed
 * mid-flight leaves a stale registration that breaks every later run. The obvious
 * recovery — `git worktree prune` — is far too blunt: it removes the registration of
 * ANY worktree whose path is not visible from where it runs, which in a container
 * that mounts the checkout at a different path means the caller's own worktree. It
 * did exactly that to me once. `git archive` touches no repo state at all, needs no
 * cleanup, cannot corrupt anything, and works on the shallow clone CI checks out.
 *
 * The WHOLE commit is exported, not just `website/`. The frontend's build graph is
 * not confined to that directory — `src/pages/connections/registry.ts` imports
 * `../../../../src/kiro_crew/connections/registry.json` — and an allowlist of the
 * paths that escape it is the shape that already rotted: `website` WAS the
 * allowlist, and the moment a PR with no reason to know this script exists added a
 * cross-tree import, every base build died with `Could not resolve …` and the gate
 * exited 2 on EVERY pull request, because the breakage lived in the base tree
 * rather than in anyone's diff. Archiving the commit costs a few seconds of tar
 * next to two ~45s vite builds and cannot rot. `node_modules` is symlinked rather
 * than installed — a second `npm ci` would cost minutes, and the base's dependency
 * set only matters if the lockfile changed, which a render gate is not the right
 * place to police.
 */
function buildBaseBundle(sha) {
  const dir = join(tmpdir(), `i18n-render-base-${sha.slice(0, 12)}`)
  rmSync(dir, { recursive: true, force: true })
  mkdirSync(dir, { recursive: true })
  out(`[i18n-render] [vs-base] exporting base tree ${sha.slice(0, 8)}`)

  const tarball = join(dir, 'base.tar')
  try {
    // The WHOLE commit, not just `website`. The frontend's build graph is not
    // confined to `website/`: `src/pages/connections/registry.ts` imports
    // `../../../../src/kiro_crew/connections/registry.json` (#1229/#1287), so a
    // `website`-only archive produced a base tree whose vite build died with
    // `Could not resolve "../../../../src/kiro_crew/connections/registry.json"`
    // — the gate then exited 2 on EVERY pull request, because the breakage lives
    // in the base tree rather than in anyone's diff.
    //
    // Deliberately not an allowlist of the extra paths. That is the shape that
    // just failed: `website` WAS the allowlist, and it rotted the moment someone
    // added a cross-tree import in a PR that had no reason to know this script
    // exists. Archiving the commit costs a few seconds of tar next to two ~45s
    // vite builds and cannot rot.
    const out = execFileSync('git', ['archive', '--format=tar', '-o', tarball, sha],
      { cwd: REPO, stdio: 'pipe' })
    void out
    execFileSync('tar', ['-xf', tarball, '-C', dir], { stdio: 'pipe' })
    rmSync(tarball, { force: true })
  } catch (err) {
    die(`could not export base tree ${sha}: ${err.message}`)
  }

  const baseWeb = join(dir, 'website')
  if (!existsSync(join(baseWeb, 'package.json'))) {
    die(`base tree ${sha} has no website/ — cannot render it`)
  }
  // `junction`, not `dir`, on Windows: creating a directory SYMLINK there needs
  // elevated privileges or Developer Mode, so a plain `dir` link fails with EPERM on
  // a stock Windows dev box and takes the whole gate down with exit 2. A junction
  // needs no privilege. It requires an absolute target, which this already is.
  symlinkSync(
    NODE_MODULES,
    join(baseWeb, 'node_modules'),
    process.platform === 'win32' ? 'junction' : 'dir',
  )

  runViteDevBuild(baseWeb, 'dist-dev', `base ${sha.slice(0, 8)}`)
  TEMP_DIRS.push(dir)
  return { dist: join(baseWeb, 'dist-dev'), baseWeb }
}

/**
 * Which surfaces exist in the BASE tree's registry.
 *
 * A surface added by this branch has no base measurement, and the base bundle has no
 * route for its URL — so whatever the base render produces for it is meaningless. It is
 * not necessarily ZERO either: the SPA resolves an unknown path through its router, so
 * the base sweep can land on a fallback page and report ITS findings. If that page is
 * busier than the new surface, the new surface's real defects read as an IMPROVEMENT
 * (`now < was`) and `[vs-base]` passes a branch that shipped them.
 *
 * So a new surface's base count is forced to 0 rather than measured. Read from the base
 * tree's own registry, which `git archive` already exported next to the bundle.
 *
 * Fails closed: an unreadable or empty base registry would silently mark EVERY surface
 * new, which is over-strict rather than dangerous, but it would also mean this gate no
 * longer knows what it is comparing — so say so instead.
 */
function baseSurfaceIds(baseWeb) {
  const reg = join(baseWeb, 'scripts', 'lib', 'i18n-surfaces.mjs')
  if (!existsSync(reg)) {
    die(`base tree has no ${reg} — cannot tell which surfaces are new on this branch`)
  }
  const src = readFileSync(reg, 'utf-8')
  const ids = new Set([...src.matchAll(/\{\s*id:\s*'([^']+)'/g)].map(m => m[1]))
  if (!ids.size) {
    die(`could not read any surface id from the base tree's ${reg}; the registry's shape `
      + 'changed, so "which surfaces are new" cannot be answered. Update baseSurfaceIds().')
  }
  return ids
}

/** Exported base trees to delete on exit. Plain directories — no repo state involved. */
const TEMP_DIRS = []
process.on('exit', () => {
  for (const dir of TEMP_DIRS) {
    try { rmSync(dir, { recursive: true, force: true }) } catch { /* best effort */ }
  }
})

async function main() {
  if (BUILD) buildDevBundle(opt('--dist', 'dist-dev'))
  if (!existsSync(new URL('index.html', `file://${DIST}`))) {
    die(`no build at ${DIST}\n  Build the DEV bundle first (en-XA is stripped from a production build):\n`
      + '    npm run i18n:render        # builds, then gates\n'
      + '    node scripts/check-i18n-render.mjs --build')
  }

  let chromium
  try {
    ({ chromium } = await import('playwright'))
  } catch {
    die('playwright is not installed. Run `npx playwright install --with-deps chromium`.')
  }

  const scanScript = browserBundle(readFileSync(SCAN_SRC, 'utf-8'))
  const dnt = JSON.parse(readFileSync(GLOSSARY, 'utf-8')).dnt
  const surfaces = ONLY_SURFACE ? SURFACES.filter(s => s.id === ONLY_SURFACE) : SURFACES
  const locales = ONLY_LOCALE ? LOCALES.filter(l => l.code === ONLY_LOCALE) : LOCALES
  if (!surfaces.length) die(`unknown --surface ${ONLY_SURFACE}`)
  if (!locales.length) die(`unknown --locale ${ONLY_LOCALE}`)

  const browser = await chromium.launch()
  let head
  let baseAll = null
  let totalIsFallback = false
  /** Surfaces this branch added — no base measurement exists for them. */
  let newSurfaces = new Set()
  try {
    head = await sweep(browser, DIST, { scanScript, dnt, surfaces, locales, label: 'HEAD' })

    // The diff-scoped half. Everything about it is derived at runtime from two
    // trees; nothing is read from a committed number, which is the whole point.
    const scope = resolveBaseScope()
    if (scope.run) {
      const { dist: baseDist, baseWeb } = buildBaseBundle(scope.sha)
      const baseIds = baseSurfaceIds(baseWeb)
      const unregistered = surfaces.map(x => x.id).filter(id => !baseIds.has(id))
      // Deliberately the SAME scanScript, surfaces and locales as the HEAD run —
      // they come from this checkout, not from the base tree. Only the BUNDLE is
      // base's. If the base tree's own scanner were used instead, any change to the
      // detector would read as a product regression (or mask one).
      const baseSweep = await sweep(browser, baseDist, {
        scanScript, dnt, surfaces, locales, label: `base ${scope.sha.slice(0, 8)}`,
      })
      baseAll = baseSweep.all
      // A surface is NEW — base 0 — only when the base bundle had no route for its
      // URL. Being absent from the base REGISTRY is not the same thing, and
      // conflating the two is a bug worth naming: adding 34 URL-reachable surfaces
      // that all existed as routes reported every one of their pre-existing
      // findings as growth, so the branch that widened the gate's aperture was
      // charged for debt it merely revealed. Zero would then be the wrong base, not
      // the honest one.
      //
      // The signal is behavioural rather than a source scan: the base sweep
      // navigated each URL, and `App.tsx` sends an unknown route through a redirect
      // (`BuiltinAppRoute` -> `<Navigate to="/chat">`, or `ChatRedirect`), so a
      // final URL that is not the requested one is proof the route was absent.
      //
      // Residual gap, stated: a query-param surface (`/settings?tab=x`) whose tab
      // the base does not know renders the DEFAULT panel at the same URL, so it
      // reads as resolved and its base count describes the wrong panel. Adding such
      // a surface in the same commit as the tab it addresses is what avoids that;
      // the redirect check cannot see it.
      newSurfaces = new Set(unregistered.filter(id => baseSweep.unresolved.has(id)))
      const measured = unregistered.filter(id => !baseSweep.unresolved.has(id))
      if (newSurfaces.size) {
        out(`[i18n-render] [vs-base] ${newSurfaces.size} surface(s) have no route on the base`
          + ` — their base count is 0, not measured: ${[...newSurfaces].join(', ')}`)
      }
      if (measured.length) {
        out(`[i18n-render] [vs-base] ${measured.length} surface(s) are newly REGISTERED but`
          + ' their route already existed on the base, so they are compared against a real'
          + ` measurement: ${measured.join(', ')}`)
      }
    } else if (scope.reason) {
      totalIsFallback = !!scope.fallback
      SKIP_REASON = scope.reason
      out(`[i18n-render] [vs-base] skipped — ${scope.reason}`)
    }
  } finally {
    await browser.close()
  }

  const partial = !!(ONLY_SURFACE || ONLY_LOCALE)
  process.exit(reconcile(head.all, baseAll, { partial, totalIsFallback, newSurfaces }))
}

/**
 * Render every surface in every locale against ONE bundle and return the findings.
 *
 * Factored out so the HEAD and base runs are provably the same measurement: a
 * difference between them can then only come from the bundles, never from how they
 * were measured.
 */
async function sweep(browser, dist, { scanScript, dnt, surfaces, locales, label }) {
  const { srv, base } = await serveDist(dist)
  /** @type {Array<{surface: string, locale: string, viewport: string, finding: object}>} */
  const all = []
  /**
   * Surfaces whose URL this bundle redirected away from, i.e. whose route it does
   * not have. Only meaningful on the BASE sweep, where it separates "this branch
   * added the route" from "this branch only added the registry entry".
   */
  const unresolved = new Set()
  let pseudoSeen = false

  try {
    for (const locale of locales) {
      // A locale may restrict itself to a subset of viewports. The DNT probe does:
      // DNT integrity is a CONTENT assertion (did a proper noun survive the
      // translator byte-identical), so re-rendering it at a second width buys
      // nothing and this gate is cap-bound. Unknown ids are a config error, not a
      // silent skip -- rendering zero viewports would exit 0 having checked nothing.
      const localeViewports = locale.viewports
        ? VIEWPORTS.filter(v => locale.viewports.includes(v.id))
        : VIEWPORTS
      if (!localeViewports.length) {
        die(`locale ${locale.code} restricts itself to viewports `
          + `[${locale.viewports.join(', ')}], none of which exist in VIEWPORTS `
          + `([${VIEWPORTS.map(v => v.id).join(', ')}]) -- fix lib/i18n-surfaces.mjs.`)
      }
      for (const viewport of localeViewports) {
        const context = await browser.newContext({
          viewport: { width: viewport.width, height: viewport.height },
          // Pin the browser tags too. Detection is only consulted when no explicit
          // choice is stored, but leaving the runner's own locale in play makes the
          // gate's result depend on the machine it ran on.
          locale: locale.code === 'en-XA' ? 'en-US' : locale.code,
          // Layout animations change measured widths mid-flight, and every width
          // this gate reports would otherwise be a race.
          reducedMotion: 'reduce',
        })
        await context.addInitScript(code => {
          localStorage.setItem('mc-lang', code)
          localStorage.setItem('mc-onboarded', '1')
          localStorage.setItem('mc-import-onboarded', '1')
        }, locale.code)

        const page = await context.newPage()
        if (VERBOSE) logPageProblems(page)
        // A crashed surface is the gate's most dangerous failure mode, because it
        // does not look like a failure: React's error boundary replaces the panel
        // with its own English message, the scan dutifully reports that message as
        // untranslated copy, and the run charges a number that has nothing to do
        // with the product. Treat any uncaught page error as INFRASTRUCTURE broken
        // (exit 2) rather than as findings.
        const pageErrors = []
        page.on('pageerror', err => pageErrors.push(String(err.message || err)))
        await stubDashboardApi(page, {
          theme: 'dark',
          extra: (path, route) => FIXTURE_OVERRIDES(locale.code, path, route),
        })

        for (const surface of surfaces) {
          pageErrors.length = 0
          await page.goto(base + surface.url, { waitUntil: 'domcontentloaded' })
          // Did this bundle actually HAVE this route? Both fallbacks in `App.tsx`
          // redirect — an unregistered builtin app hits
          // `BuiltinAppRoute`'s `<Navigate to="/chat">`, an unknown top-level path
          // hits `ChatRedirect` — so a final URL that is not the requested one
          // means the route did not exist here. `[vs-base]` needs this to tell a
          // surface whose ROUTE is new from one that merely was not registered
          // yet: the second kind is measurable on the base and its findings are
          // pre-existing debt, not something the branch added.
          if (new URL(page.url()).pathname + new URL(page.url()).search !== surface.url) {
            unresolved.add(surface.id)
          }
          // Wait for the shell to actually paint. A fixed sleep raced the first
          // render and made the gate report zero findings on a surface it never
          // saw — the quietest possible false pass. The predicate is deliberately
          // locale-agnostic (text VOLUME, not any particular string) so it works
          // identically under the pseudolocale and under zh-CN.
          try {
            await page.waitForFunction(
              () => document.body.innerText.trim().length > 100, { timeout: 15000 },
            )
          } catch {
            die(`[${label}] ${surface.url} rendered nothing in 15s — the fixtures are probably `
              + 'the wrong shape for this surface (see lib/boot-api.mjs for the two shapes that '
              + 'error-boundary the whole shell). Re-run with --verbose to see the page errors.')
          }
          // Panels that fetch after mount need a beat more; a half-rendered surface
          // under-reports rather than failing loudly.
          await page.waitForTimeout(surface.settle || 250)
          // Every width this gate reports is a text measurement, and text measures
          // differently in the fallback face than in the real one. Scanning before
          // the webfonts land made the layout bucket differ by 2 between identical
          // runs (artifacts.layout 4 vs 2), which would have made the whole gate
          // flaky rather than wrong. Two rAF ticks after that let the resulting
          // reflow finish before anything is read.
          await page.evaluate(() => document.fonts.ready)
          await page.evaluate(() => new Promise(r => requestAnimationFrame(
            () => requestAnimationFrame(() => r(null)),
          )))
          if (pageErrors.length) {
            die(`[${label}] ${surface.url} (${locale.code}) threw while rendering, so its `
              + 'findings would be the error boundary\'s own English copy rather than the '
              + `product's:\n${pageErrors.slice(0, 3).map(e => `      ${e}`).join('\n')}`
              + '\n    Fix the fixture shape in FIXTURE_OVERRIDES, or drop the surface from '
              + 'lib/i18n-surfaces.mjs with a comment saying why.')
          }
          await page.addScriptTag({ content: scanScript })

          if (locale.mode === 'pseudo' && !pseudoSeen) {
            pseudoSeen = await assertPseudoActive(page, surface)
          }
          const findings = await page.evaluate(
            o => window.__I18N_SCAN.scanDocument(o),
            { mode: locale.mode, minLetters: 1, dnt: locale.mode === 'real' ? dnt : [] },
          )
          for (const finding of findings) {
            // A DNT-probe locale contributes ONLY its zero-tolerance DNT findings.
            // Dropping the rest is what makes the reduced locale set honest: a real
            // locale rendered at one viewport measures `layout`/`latent` partially,
            // and a partial measurement folded into the ledger LOWERS the recorded
            // debt -- the silent-understatement failure `AGENTS.md` warns about, and
            // worse than not measuring it. `tally()` already routes `kind: 'dnt'`
            // out of the buckets, so the probe's findings stay zero-tolerance while
            // the buckets come purely from the fully-rendered pseudolocale.
            if (locale.dntOnly && finding.kind !== 'dnt') continue
            all.push({ surface: surface.id, locale: locale.code, viewport: viewport.id, finding })
          }
        }
        await context.close()
      }
    }
  } finally {
    srv.close()
  }

  if (locales.some(l => l.mode === 'pseudo') && !pseudoSeen) {
    die(`[${label}] the en-XA catalog never rendered — this build is not a DEV build, so the `
      + 'text assertions would have passed against English.\n'
      + '    node scripts/check-i18n-render.mjs --build')
  }
  // DNT is not asserted here — see the `LOCALES` docstring in
  // `lib/i18n-surfaces.mjs`. It cannot run under the pseudolocale (every term
  // renders accented by design), and with no real locale in the list there is
  // nothing for it to run against, so it lives in `check-dnt-catalogs.mjs` as a
  // value comparison over all 9 shipped catalogs. The guard that used to stand here
  // required a real locale precisely so this assertion could not go quiet; that
  // requirement now belongs to that script, which the i18n runner fails closed on.
  return { all, unresolved }
}

/**
 * Prove the pseudolocale is live before trusting a single text assertion.
 *
 * Without this the gate's most likely failure is also its quietest: a production
 * bundle renders plain English, every string is un-accented but also outside any
 * `[` … `]` unit... and the scan happily reports whatever English text it finds as
 * leaks, or — if the ledger was recorded the same way — reports nothing at all.
 */
async function assertPseudoActive(page, surface) {
  try {
    await page.waitForFunction(
      () => /[\u00C0-\u024F]/.test(document.body.innerText)
        && document.body.innerText.includes('['),
      { timeout: 10000 },
    )
    return true
  } catch {
    if (VERBOSE) out(`[i18n-render] no accented text on ${surface.id}`)
    return false
  }
}

/**
 * The whole-repo census, grouped the way every enforced number is grouped.
 *
 * Signature counts are kept because they are the only per-signature view that exists —
 * the record and `[vs-base]` are per-surface × bucket, so `76 fragment/multi-unit`
 * appears nowhere else, and it is what tells you whether the next batch is "extract
 * strings" or "add min-w-0". The per-signature EXAMPLE is not kept: its `fix:` line is
 * a constant for five of the seven signatures (so printing it every run is
 * documentation in a log), and its `e.g.` DOM path names one arbitrary row out of
 * 1366. Detail moves to `--verbose`, where it can carry source locations.
 */
function report(all) {
  if (!all.length) {
    out('[i18n-render] no findings')
    return
  }
  const byBucket = { text: new Map(), latent: new Map(), layout: new Map() }
  const worst = new Map()
  for (const rec of all) {
    const { locale, finding } = rec
    // DNT is not a count — it is zero-tolerance and reported on its own.
    if (finding.kind === 'dnt') continue
    const key = `${finding.kind}/${finding.signature}`
    const m = byBucket[bucketOf(finding)]
    m.set(key, (m.get(key) || 0) + 1)
    const ratio = finding.ratio || 0
    if (ratio > (worst.get(key)?.ratio || 0)) worst.set(key, { locale, ratio })
  }
  const total = BUCKETS.reduce(
    (n, b) => n + [...byBucket[b].values()].reduce((x, y) => x + y, 0), 0,
  )
  // Column widths come from the data: the `worst:` column has to line up, and a
  // signature name longer than today's cannot be allowed to shove it out of true.
  const sigWidth = Math.max(
    0, ...BUCKETS.flatMap(b => [...byBucket[b].keys()].map(k => k.length)),
  ) + 6
  out(`\n[i18n-render] ${total} findings — grouped the way the record and [vs-base] count them\n`)
  for (const bucket of ['text', 'latent', 'layout']) {
    const m = byBucket[bucket]
    const sum = [...m.values()].reduce((x, y) => x + y, 0)
    out(`  ${bucket.padEnd(8)}${String(sum).padStart(5)}  ${BUCKET_MEANING[bucket]}`)
    for (const [key, count] of [...m].sort((a, b) => b[1] - a[1])) {
      const w = worst.get(key)
      // Indented well past the bucket's own number so a signature row can never be
      // misread as a bucket row — they are different vocabularies and the whole point
      // of this block is that the reader can tell which one they are looking at.
      // `worst:` is printed only where the ratio is real: on a text row `worst: en-XA`
      // is a tautology, because leaks are scanned under the pseudolocale and nowhere else.
      const row = w && w.ratio ? `${key.padEnd(sigWidth)}worst: ${w.locale} @ ${w.ratio}x` : key
      out(`                ${String(count).padStart(6)}  ${row}`)
    }
  }
  out('')
  if (VERBOSE) {
    const groups = groupForDisplay(all)
    group(
      `[i18n-render] every finding — ${all.length} at ${groups.length} distinct site(s)`,
      groups.flatMap(({ rec, count, details }) => [...fmtFinding(rec, '  ', count, details), '']),
    )
  } else {
    out('  Per-finding source locations: --verbose.')
  }
}

/**
 * Print the verdict and return the exit code.
 *
 * The decision itself lives in `lib/render-verdict.mjs` as a pure function over
 * plain numbers; this only formats it. Two things are measured, and only one of them
 * can fail:
 *
 * 1. **[vs-base] — the gate.** Renders the base tree with the SAME scanner and fails
 *    on any per-surface increase. Both sides are derived at runtime, so there is
 *    nothing to re-snapshot and nothing to absorb a regression with: the bypass that
 *    made the old bidirectional ratchets decorative (`195904c` shipped 113
 *    untranslated strings while RAISING `_total`, green) does not exist here.
 * 2. **the ledger — a report.** Per-surface absolute debt. It is printed, never
 *    enforced: a stored total is written by whichever branch measured it last, so two
 *    branches that never touch the same file can combine into a number neither
 *    produced, and no diff exists that anyone could fix. See `lib/render-verdict.mjs`
 *    for the incident that proved it (#1107 + #985 -> `projects.latent 16 -> 24`).
 */
function reconcile(all, baseAll, { partial, totalIsFallback = false, newSurfaces = new Set() }) {
  const surfaceIds = SURFACES.map(s => s.id)
  const { counts, dnt: dntFindings } = tally(all, surfaceIds)

  if (UPDATE) {
    if (partial) die('--update needs the full run; drop --surface/--locale')
    const ledger = {
      _comment: LEDGER_COMMENT,
      _total: totals(counts),
      surfaces: counts,
    }
    writeFileSync(LEDGER, `${JSON.stringify(ledger, null, 2)}\n`)
    const t = ledger._total
    out(`[i18n-render] ledger written: text=${t.text} layout=${t.layout} latent=${t.latent}`)
    return 0
  }

  // A missing record is a HARD failure, not a soft `null`. It is a committed file, so
  // its absence is a broken checkout or a deletion, never a normal state — and in the
  // one case where the record is the guard (no base to diff) a `null` would leave the
  // run with nothing to check but DNT while still exiting 0. That is exactly the
  // "stops guarding silently" mode this whole change removes. `--update` seeds it and
  // returns above, before this line, so bootstrapping still works.
  if (!existsSync(LEDGER)) {
    die(`no debt record at ${LEDGER}. Seed it with --build --update.`)
  }
  const ledger = JSON.parse(readFileSync(LEDGER, 'utf-8'))

  const baseCounts = baseAll ? tally(baseAll, surfaceIds).counts : null
  const { failed, enforced, dnt, grew, shrank, above, below } = decide({
    counts,
    baseCounts,
    ledger,
    surfaceIds,
    dnt: dntFindings,
    partial,
    onlySurface: ONLY_SURFACE,
    totalIsFallback,
    newSurfaces,
  })

  // ---- the verdict, FIRST -----------------------------------------------------
  // It used to sit at line 49 of 96, between a 45-line census and a 46-line drift
  // list. In a collapsed Actions log the reader lands on the reports and scrolls
  // past the one line that decided the run. Verdict first, evidence after.
  const ht = totals(counts)
  const live = `text=${ht.text} layout=${ht.layout} latent=${ht.latent}`
  if (dnt.length) {
    out(`\n[i18n-render] FAIL [dnt] — ${dnt.length} do-not-translate violation(s), zero tolerance`)
  }
  if (grew.length) {
    out(`\n[i18n-render] FAIL [vs-base] — this branch ADDS render-time i18n defects`)
  } else if (enforced) {
    const bt = totals(baseCounts)
    out(`\n[i18n-render] PASS [vs-base] — no surface got worse`)
    out(`              base ${`text=${bt.text} layout=${bt.layout} latent=${bt.latent}`}  ->  head ${live}`)
  } else if (totalIsFallback) {
    out(`\n[i18n-render] ${above.length ? 'FAIL' : 'PASS'} [debt record] — NO base commit to diff, so the`
      + '\n              record is the guard for this run instead of a report.')
  } else {
    out('\n[i18n-render] NOT ENFORCED — no base was rendered and none was asked for'
      + `\n              (${SKIP_REASON || 'report-only run'}). DNT is still zero-tolerance.`)
  }
  if (!failed) out(`[i18n-render] ${live} (goal 0)`)

  // ---- what to change, for each failure ---------------------------------------
  if (dnt.length) {
    out('\n[i18n-render] [dnt] a do-not-translate term was altered:\n')
    for (const { rec, count, details } of groupForDisplay(dnt).slice(0, 20)) {
      for (const l of fmtFinding(rec, '    ', count, details)) out(l)
      out('')
    }
  }

  if (grew.length) {
    out('\n[i18n-render] [vs-base] surfaces that got worse:\n')
    for (const g of grew) out(`    ${fmtDelta(g)}                (base -> head)`)
    // A count tells the author a number moved; it does not tell them which line to
    // edit. These are the individual findings present at HEAD and absent at base.
    const added = addedFindings(all, baseAll || [], grew)
    if (added.length) {
      const exact = identityIsExact(added) && identityIsExact(baseAll || [])
      const groups = groupForDisplay(added)
      const same = groups.length === added.length
      out(`\n  the ${added.length} finding(s) this branch added`
        + `${same ? '' : `, at ${groups.length} distinct site(s)`}`
        + `${exact ? '' : ' (approximate — see below)'}:\n`)
      for (const { rec, count, details } of groups.slice(0, 25)) {
        for (const l of fmtFinding(rec, '    ', count, details)) out(l)
        out('')
      }
      if (groups.length > 25) out(`    ... and ${groups.length - 25} more site(s) (--verbose)\n`)
      if (!exact) {
        out('  Some findings carry no source location, so identity fell back to the DOM'
          + '\n  path, which is positional — inserting one wrapper can make an untouched'
          + '\n  finding look new. The per-surface counts above are exact regardless.\n')
      }
    }
    out('  Measured against the base commit, not against a committed number — there is'
      + '\n  nothing to re-snapshot.\n')
  }

  report(all)

  if (shrank.length && enforced && !grew.length) {
    out('\n[i18n-render] [vs-base] this branch FIXES:\n')
    for (const s of shrank) out(`    ${fmtDelta(s)}`)
    out('')
  }

  // ---- the debt record --------------------------------------------------------
  // An increase can come from two branches that never touched the same file combining
  // after merge — a number no diff produced and nobody can fix by fixing their own
  // change — so it is a report unless it is the only check available.
  if (above.length) {
    if (totalIsFallback) {
      out('\n[i18n-render] above the debt record, and there was no diff to check instead:\n')
      for (const a of above) out(`    ${fmtDelta(a)}                (record -> live)`)
      out('')
    } else {
      out(`\n[i18n-render] above the debt record on ${above.length} entr(ies) — a report; [vs-base] decided this run.`)
      group('[i18n-render] record -> live, above (report)', above.map(a => `  ${fmtDelta(a)}`))
    }
  }
  if (below.length) {
    // Two lines instead of forty. Every line of the old list said the same thing, on
    // every run, for every PR, and nothing obliges anyone to act — so it trained
    // readers to skip a block that IS the verdict on the no-base path. The aggregate
    // states the one fact that matters: this slack is the fallback's blind spot.
    const gap = { text: 0, layout: 0, latent: 0 }
    for (const b of below) gap[b.bucket] += b.was - b.now
    const surfaces = new Set(below.map(b => b.surface)).size
    out(`\n[i18n-render] debt record is STALE on ${surfaces}/${surfaceIds.length} surfaces — overstates by `
      + `text +${gap.text}, layout +${gap.layout}, latent +${gap.latent}.`)
    out('              That slack is exactly the blind spot of the no-base fallback.'
      + ' Refresh: --build --update')
    group(
      `[i18n-render] record -> live, per surface (${below.length} entries)`,
      below.map(b => `  ${fmtDelta(b)}`),
    )
  }
  return failed ? 1 : 0
}

main().catch(err => {
  out(String(err && err.stack ? err.stack : err))
  process.exit(2)
})
