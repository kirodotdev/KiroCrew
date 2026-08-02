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
 *   - **[vs-base] is the gate.** With `I18N_BASE_REF` set, this renders the BASE
 *     commit too — same scanner, same surfaces, same locales, only the bundle
 *     differs — and fails on any per-surface increase. It reads no committed
 *     number, so there is nothing to re-snapshot and nothing to absorb a
 *     regression with. That is what licenses the ledger below to be upward-only
 *     under `website/AGENTS.md`'s rule.
 *   - the LEDGER (`src/i18n/render-baseline.json`) is a debt record and the
 *     backstop for a push to `main`, where there is no base to diff. Upward-only,
 *     keyed per surface, allowed to sit above reality; a decrease is reported,
 *     never required. Goal 0 for every entry.
 *
 * A finer ledger alone would NOT have been enough, and it is worth being precise
 * about why: per-surface keys only shrink the range a regression can hide within.
 * Measured — inflate `settings-about.text` from 28 to 100, add 18 real defects, and
 * the ledger reports an *improvement* while [vs-base] fails with `28 -> 46 (+18)`.
 *
 * Usage:
 *   npm run i18n:render                                   # build + gate (+ vs-base)
 *   node scripts/check-i18n-render.mjs --build --update    # build + reseed ledger
 *   node scripts/check-i18n-render.mjs                     # gate an existing dist-dev
 *   node scripts/check-i18n-render.mjs --no-vs-base        # ledger only, skip base render
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
import { SURFACES, LOCALES, VIEWPORTS } from './lib/i18n-surfaces.mjs'
import { browserBundle } from './lib/render-scan.mjs'

const SCAN_SRC = fileURLToPath(new URL('./lib/render-scan.mjs', import.meta.url))
const LEDGER = fileURLToPath(new URL('../src/i18n/render-baseline.json', import.meta.url))
const GLOSSARY = fileURLToPath(new URL('../src/i18n/glossary.json', import.meta.url))
/** Repo root — `website/`'s parent, where every git call is rooted. */
const REPO = fileURLToPath(new URL('../..', import.meta.url))

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
const DIST = fileURLToPath(new URL(`../${opt('--dist', 'dist-dev')}/`, import.meta.url))
const ONLY_SURFACE = opt('--surface', '')
const ONLY_LOCALE = opt('--locale', '')

const die = msg => {
  console.error(`\n[i18n-render] ${msg}\n`)
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
    console.error(`${res.stdout || ''}\n${res.stderr || ''}`.trim().split('\n').slice(-25).join('\n'))
    die(`[${label}] vite build failed (exit ${res.status})`)
  }
  console.log(`[i18n-render] [${label}] built in ${((Date.now() - started) / 1000).toFixed(0)}s`)
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
 *   - no `I18N_BASE_REF` means there is genuinely no base (a push to `main`), so the
 *     ledger runs alone.
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
  if (UPDATE) return { run: false, reason: '--update only reseeds the ledger' }
  const baseRef = process.env.I18N_BASE_REF
  if (!baseRef) {
    return { run: false, reason: 'I18N_BASE_REF is unset, so there is no branch to diff (push to main)' }
  }

  const git = args => execFileSync('git', args, { cwd: REPO, encoding: 'utf-8' }).trim()
  try {
    git(['rev-parse', '--verify', `${baseRef}^{commit}`])
  } catch {
    die(`cannot resolve ${baseRef}. The diff-scoped render gate needs the base ref; fetch it\n`
      + '    before running this script, or unset I18N_BASE_REF to check only the ledger.')
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

  // Nothing renderable changed, so the base render is guaranteed identical and the
  // second build+sweep would be ~2.5 minutes of CI for a known answer. Scoped to what
  // can actually alter a rendered surface: app source, the catalogs, the build config.
  const changed = git(['diff', '--name-only', `${sha}`, 'HEAD']).split('\n').filter(Boolean)
  const renderable = changed.filter(f => /^website\/(src\/|index\.html|vite\.config|tailwind|postcss|package\.json)/.test(f))
  if (!renderable.length) {
    return { run: false, reason: `no renderable file changed vs ${sha.slice(0, 8)} (${changed.length} file(s) in diff)` }
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
 * Only `website/` is exported: nothing in its build config reaches outside it.
 * `node_modules` is symlinked rather than installed — a second `npm ci` would cost
 * minutes, and the base's dependency set only matters if the lockfile changed, which
 * a render gate is not the right place to police.
 */
function buildBaseBundle(sha) {
  const dir = join(tmpdir(), `i18n-render-base-${sha.slice(0, 12)}`)
  rmSync(dir, { recursive: true, force: true })
  mkdirSync(dir, { recursive: true })
  console.log(`[i18n-render] [vs-base] exporting base tree ${sha.slice(0, 8)}`)

  const tarball = join(dir, 'base.tar')
  try {
    const out = execFileSync('git', ['archive', '--format=tar', '-o', tarball, sha, 'website'],
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
    join(fileURLToPath(new URL('..', import.meta.url)), 'node_modules'),
    join(baseWeb, 'node_modules'),
    process.platform === 'win32' ? 'junction' : 'dir',
  )

  runViteDevBuild(baseWeb, 'dist-dev', `base ${sha.slice(0, 8)}`)
  TEMP_DIRS.push(dir)
  return join(baseWeb, 'dist-dev')
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
  try {
    head = await sweep(browser, DIST, { scanScript, dnt, surfaces, locales, label: 'HEAD' })

    // The diff-scoped half. Everything about it is derived at runtime from two
    // trees; nothing is read from a committed number, which is the whole point.
    const scope = resolveBaseScope()
    if (scope.run) {
      const baseDist = buildBaseBundle(scope.sha)
      // Deliberately the SAME scanScript, surfaces and locales as the HEAD run —
      // they come from this checkout, not from the base tree. Only the BUNDLE is
      // base's. If the base tree's own scanner were used instead, any change to the
      // detector would read as a product regression (or mask one).
      baseAll = (await sweep(browser, baseDist, {
        scanScript, dnt, surfaces, locales, label: `base ${scope.sha.slice(0, 8)}`,
      })).all
    } else if (scope.reason) {
      console.log(`[i18n-render] [vs-base] skipped — ${scope.reason}`)
    }
  } finally {
    await browser.close()
  }

  report(head.all)
  const partial = !!(ONLY_SURFACE || ONLY_LOCALE)
  process.exit(reconcile(head.all, baseAll, { partial }))
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
  let pseudoSeen = false

  try {
    for (const locale of locales) {
      for (const viewport of VIEWPORTS) {
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
        // untranslated copy, and the ledger absorbs a number that has nothing to do
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
  return { all }
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
    if (VERBOSE) console.log(`[i18n-render] no accented text on ${surface.id}`)
    return false
  }
}

/** Group findings and print the prescriptive output Phase 5 item 3 requires. */
function report(all) {
  if (!all.length) {
    console.log('[i18n-render] no findings')
    return
  }
  /** @type {Map<string, {count: number, worst: {locale: string, ratio: number}, sample: object}>} */
  const bySignature = new Map()
  for (const { locale, finding } of all) {
    const key = `${finding.kind}/${finding.signature}`
    const prev = bySignature.get(key)
    const ratio = finding.ratio || 0
    if (!prev) {
      bySignature.set(key, { count: 1, worst: { locale, ratio }, sample: finding })
    } else {
      prev.count++
      if (ratio > prev.worst.ratio) prev.worst = { locale, ratio }
    }
  }
  console.log('\n[i18n-render] findings by signature\n')
  for (const [key, v] of [...bySignature].sort((a, b) => b[1].count - a[1].count)) {
    console.log(`  ${String(v.count).padStart(5)}  ${key}`)
    console.log(`         worst: ${v.worst.locale}${v.worst.ratio ? ` @ ${v.worst.ratio}x` : ''}`)
    console.log(`         e.g.:  ${v.sample.path}`)
    console.log(`                ${JSON.stringify(v.sample.text)}`)
    if (v.sample.fixedAncestor) console.log(`         ancestor: ${v.sample.fixedAncestor}`)
    console.log(`         fix:   ${v.sample.fix}\n`)
  }
  if (VERBOSE) {
    console.log('[i18n-render] every finding\n')
    for (const { surface, locale, viewport, finding } of all) {
      console.log(`  ${surface} ${locale} ${viewport} ${finding.signature} ${finding.path}`)
      console.log(`      ${JSON.stringify(finding.text)}${finding.detail ? ` -- ${finding.detail}` : ''}`)
    }
  }
}

/**
 * Ledger buckets.
 *
 *   text   — never reached a catalog, or was assembled from several keys.
 *   layout — the text does not fit RIGHT NOW, in some shipped locale.
 *   latent — a pattern that is silently broken but not yet visible, which today
 *            means `ellipsis-with-flex-parent`: `text-overflow` cannot apply while
 *            a flex child's `min-width` is `auto`, so the truncation those sites
 *            think they have does nothing and the text pushes its sibling out
 *            instead. It is kept separate because there are ~460 of them and
 *            folding them into `layout` would bury the handful of real overflows
 *            under a uniform `min-w-0` cleanup that belongs to its own PR.
 */
const BUCKETS = ['text', 'layout', 'latent']

const bucketOf = finding => {
  if (finding.kind !== 'layout') return 'text'
  return finding.signature === 'ellipsis-with-flex-parent' ? 'latent' : 'layout'
}

/** Compare against the ledger, or rewrite it under `--update`. */
/**
 * Two independent checks, in decreasing order of strength.
 *
 * 1. **[vs-base]** — the real gate. Renders the base tree with the SAME scanner and
 *    fails on any per-surface increase. It reads no committed number, so there is
 *    nothing to re-snapshot and nothing to absorb a regression with: the bypass that
 *    made the old bidirectional ratchets decorative (`195904c` shipped 113
 *    untranslated strings while RAISING `_total`, green) does not exist here.
 * 2. **ledger** — upward-only, per surface. With [vs-base] carrying enforcement this
 *    is a debt record and a backstop for the push-to-`main` case where there is no
 *    base to diff. It is allowed to sit above reality; a decrease is reported, never
 *    required.
 */
function reconcile(all, baseAll, { partial }) {
  const tally = records => {
    const c = {}
    for (const s of SURFACES) c[s.id] = Object.fromEntries(BUCKETS.map(b => [b, 0]))
    const dnt = []
    for (const { surface, finding, locale } of records) {
      if (finding.kind === 'dnt') { dnt.push({ surface, locale, finding }); continue }
      c[surface][bucketOf(finding)]++
    }
    return { counts: c, dnt }
  }

  const { counts, dnt: dntFindings } = tally(all)
  const zero = () => Object.fromEntries(BUCKETS.map(b => [b, 0]))
  const totals = c => Object.fromEntries(
    BUCKETS.map(b => [b, Object.values(c).reduce((n, x) => n + x[b], 0)]),
  )

  if (UPDATE) {
    if (partial) die('--update needs the full run; drop --surface/--locale')
    const ledger = {
      _comment: 'Phase 5 render-time gate. Findings from rendering the real DEV build under '
        + 'en-XA (text) and the shipped locales (layout/latent). This is a DEBT RECORD, not '
        + 'the enforcement: [vs-base] renders the base tree and fails on any per-surface '
        + 'increase without reading this file, so nothing here can absorb a regression. '
        + 'Upward-only and keyed per surface; a decrease is reported, never required. The '
        + 'goal for every entry is 0. Regenerate with `--update` and only commit a decrease.',
      _total: totals(counts),
      surfaces: counts,
    }
    writeFileSync(LEDGER, `${JSON.stringify(ledger, null, 2)}\n`)
    const t = ledger._total
    console.log(`[i18n-render] ledger written: text=${t.text} layout=${t.layout} latent=${t.latent}`)
    return 0
  }

  if (!existsSync(LEDGER)) die(`no ledger at ${LEDGER}. Seed it with --update.`)
  const ledger = JSON.parse(readFileSync(LEDGER, 'utf-8'))
  let failed = false

  if (dntFindings.length) {
    failed = true
    console.error(`\n[i18n-render] [dnt] ${dntFindings.length} do-not-translate violation(s) — zero tolerance\n`)
    for (const { surface, locale, finding } of dntFindings.slice(0, 20)) {
      console.error(`  ${surface} (${locale}): ${finding.detail}`)
      console.error(`      ${finding.path}\n`)
    }
  }

  // ---- [vs-base]: the strict, stateless half --------------------------------
  if (baseAll) {
    const { counts: baseCounts } = tally(baseAll)
    const grew = []
    const shrank = []
    for (const s of SURFACES) {
      for (const bucket of BUCKETS) {
        const now = counts[s.id][bucket]
        const was = baseCounts[s.id][bucket]
        if (now > was) grew.push(`${s.id}.${bucket}: ${was} -> ${now} (+${now - was})`)
        else if (now < was) shrank.push(`${s.id}.${bucket}: ${was} -> ${now} (-${was - now})`)
      }
    }
    if (grew.length) {
      failed = true
      console.error('\n[i18n-render] [vs-base] this branch ADDS render-time i18n defects:\n')
      for (const g of grew) console.error(`  ${g}`)
      console.error('\n  These are measured against the base commit, not against a committed'
        + '\n  number — there is nothing to re-snapshot. Fix the findings above.\n')
    } else {
      const bt = totals(baseCounts)
      const ht = totals(counts)
      console.log(`[i18n-render] [vs-base] OK — no surface got worse `
        + `(base text=${bt.text}/layout=${bt.layout}/latent=${bt.latent} → `
        + `head text=${ht.text}/layout=${ht.layout}/latent=${ht.latent})`)
      if (shrank.length) {
        console.log('\n[i18n-render] [vs-base] this branch FIXES:\n')
        for (const s of shrank) console.log(`  ${s}`)
        console.log('')
      }
    }
  }

  // ---- ledger: upward-only debt record --------------------------------------
  const regressions = []
  const improvements = []
  for (const s of SURFACES) {
    if (partial && ONLY_SURFACE && s.id !== ONLY_SURFACE) continue
    const ceiling = (ledger.surfaces && ledger.surfaces[s.id]) || zero()
    for (const bucket of BUCKETS) {
      const now = counts[s.id][bucket]
      const was = ceiling[bucket] || 0
      // A partial run cannot see every locale, so it can only ever under-count.
      // Reporting an "improvement" from that would invite a bogus ledger drop.
      if (partial && now < was) continue
      if (now > was) regressions.push(`${s.id}.${bucket}: ${was} -> ${now}`)
      else if (now < was) improvements.push(`${s.id}.${bucket}: ${was} -> ${now}`)
    }
  }

  if (regressions.length) {
    failed = true
    console.error('\n[i18n-render] these surfaces exceed the ledger (upward-only):\n')
    for (const r of regressions) console.error(`  ${r}`)
    console.error('\n  Fix the findings above. Do NOT run --update to absorb them.\n')
  }
  if (improvements.length) {
    // NOT a failure. An upward-only ledger fails on growth only; failing on a
    // decrease is the bidirectional behaviour #1060 removed, and it would turn any
    // merge that fixes strings — #1040, say — into a red main until someone
    // remembered to re-run --update.
    console.log('\n[i18n-render] ledger sits above reality — lower it when convenient:\n')
    for (const i of improvements) console.log(`  ${i}`)
    console.log('\n  node scripts/check-i18n-render.mjs --build --update\n')
  }
  if (!failed) {
    const t = totals(counts)
    console.log(`[i18n-render] OK — text=${t.text} layout=${t.layout} latent=${t.latent} (goal 0)`)
  }
  return failed ? 1 : 0
}

main().catch(err => {
  console.error(err)
  process.exit(2)
})
