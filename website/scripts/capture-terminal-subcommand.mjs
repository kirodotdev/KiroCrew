/**
 * Screenshot + video harness for the terminal panel's SUBCOMMAND/FLAG completion.
 *
 * What is real here and what is not, stated up front, because a completion menu
 * is easy to fake convincingly:
 *
 *  - REAL: the built SPA (`website/dist`), a real `xterm.js` instance, the real
 *    `TerminalCompletion` component reading the real screen buffer, and the real
 *    key handling. The menu in these shots is produced by the shipped code.
 *  - REAL DATA: every entry comes from output captured verbatim from
 *    `gh __complete pr ""`, `gh __complete pr create --` and
 *    `git --list-cmds=…` on a developer machine (gh 2.96.0 / git 2.47.3). The
 *    descriptions are the tools' own.
 *  - SCRIPTED: the PTY. `page.routeWebSocket` stands in for the shell — it prints
 *    a prompt and echoes what is typed — because a capture that needed a live
 *    gateway, a real shell and an installed `gh` would not be reproducible in CI.
 *    `POST /api/terminal/complete` is answered from the fixtures above rather
 *    than by running a probe, for the same reason.
 *
 * So these shots prove the FRONTEND end to end against real protocol data. The
 * backend's own protocol handling is proven by `test/test_terminal_commands.py`,
 * which parses the same captured output and spawns real processes for the probe
 * path.
 *
 * OUTPUT: eight stills, a side-panel parity still, a post-acceptance still, and a
 * webm. The webm is an INTERMEDIATE — the artifact committed under
 * `.github/screenshots/` is a GIF, because GitHub only embeds a video player for
 * files uploaded to its own asset host, so a repo-hosted webm renders as a link
 * while a GIF animates inline. This script prints the exact `ffmpeg` line to
 * produce it, with the trim offset and crop rectangle MEASURED from this run
 * rather than hardcoded (the SPA's boot time and the panel's geometry both move,
 * and a stale offset yields a GIF whose first frame — its poster frame — is a
 * blank booting page).
 *
 * Usage: node scripts/capture-terminal-subcommand.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/terminal-subcommand'

const PROMPT = '\x1b[36m~/work/KiroCrew\x1b[0m \x1b[35m(main)\x1b[0m \x1b[32m❯\x1b[0m '

/** Captured verbatim from `gh __complete pr ""` (gh 2.96.0). */
const GH_PR = [
  ['checkout', 'Check out a pull request in git'],
  ['checks', 'Show CI status for a single pull request'],
  ['close', 'Close a pull request'],
  ['comment', 'Add a comment to a pull request'],
  ['create', 'Create a pull request'],
  ['diff', 'View changes in a pull request'],
  ['edit', 'Edit a pull request'],
  ['list', 'List pull requests in a repository'],
  ['lock', 'Lock pull request conversation'],
  ['merge', 'Merge a pull request'],
  ['ready', 'Mark a pull request as ready for review'],
  ['reopen', 'Reopen a pull request'],
  ['review', 'Add a review to a pull request'],
  ['status', 'Show status of relevant pull requests'],
  ['update-branch', 'Update a pull request branch'],
  ['view', 'View a pull request'],
]

/** Captured verbatim from `gh __complete pr create --`. */
const GH_PR_CREATE_FLAGS = [
  ['--assignee', 'Assign people by their `login`. Use "@me" to self-assign.'],
  ['--base', 'The `branch` into which you want your code merged'],
  ['--body', 'Body for the pull request'],
  ['--body-file', 'Read body text from `file` (use "-" to read from standard input)'],
  ['--draft', 'Mark pull request as a draft'],
  ['--dry-run', 'Print details instead of creating the PR. May still push git changes.'],
  ['--fill', 'Use commit info for title and body'],
  ['--head', 'The `branch` that contains commits for your pull request'],
  ['--label', 'Add labels by `name`'],
  ['--title', 'Title for the pull request'],
]

/** Captured from `git --list-cmds=list-mainporcelain,others,list-complete,alias`.
 *  git supplies NO descriptions — the shots must show that honestly. */
const GIT_C = [
  'checkout', 'cherry-pick', 'citool', 'clean', 'clone', 'commit', 'cherry', 'config',
]


// `at: 0` mirrors the wire form: command matching is a prefix match, and the
// field is what makes the client underline the typed span.
const sub = ([name, desc]) => ({ name, desc, kind: 'sub', at: 0 })
const flag = ([name, desc]) => ({ name, desc, kind: 'flag', at: 0 })

/**
 * The fixture answer for one request body, mirroring what the backend would do:
 * pick the listing for this argv position, then narrow it by the typed prefix.
 */
function answer({ argv = [], token = '' }) {
  const path = argv.join(' ')
  const wantFlags = token.startsWith('-')
  let entries = []
  if (path === 'gh pr' && !wantFlags) entries = GH_PR.map(sub)
  else if (path === 'gh pr create' && wantFlags) entries = GH_PR_CREATE_FLAGS.map(flag)
  else if (path === 'git' && !wantFlags) {
    entries = GIT_C.map(n => ({ name: n, desc: '', kind: 'sub', at: 0 }))
  }
  // No `git … --` case on purpose: the backend refuses git FLAG completion, because
  // that probe could execute a `!` shell alias. A fixture for it would show the UI
  // doing something production does not.
  const lowered = token.toLowerCase()
  entries = entries.filter(e => e.name.toLowerCase().startsWith(lowered))
  return { dir: null, prefix: token, entries, truncated: false }
}

const slots = [
  { key: 's1', title: 'Terminal completion demo', messages: 2, running: false, agent: 'kirocrew', mode: '', created: '2026-08-06T01:00:00Z', last_ts: '2026-08-06T04:00:00Z', folder_id: '' },
]

/**
 * Scripted PTY: prints a prompt, echoes what is typed, honours DEL.
 *
 * Two details that are easy to get wrong and produce a blank terminal:
 *
 *  - the frames must be `Buffer`s. A bare `Uint8Array` is accepted by
 *    `WebSocketRoute.send` and then never arrives as binary, so xterm renders
 *    nothing and the failure looks like a timing problem.
 *  - the prompt must be sent AFTER the client's socket `onopen`, because
 *    `terminalRegistry.connect` calls `term.reset()` there (the real backend
 *    replays scrollback on connect, so the screen is cleared first). A prompt
 *    sent on the route callback is wiped by that reset. The client's first
 *    control frame is the reliable "reset has happened" signal; the timer is the
 *    fallback for a pane laid out at 0x0, which sends no resize.
 */
async function stubPty(page) {
  await page.routeWebSocket(/\/api\/ws\/terminal\//, ws => {
    let greeted = false
    const greet = () => {
      if (greeted) return
      greeted = true
      ws.send(Buffer.from('\r\n' + PROMPT, 'utf8'))
    }
    setTimeout(greet, 600)
    ws.onMessage(msg => {
      if (typeof msg === 'string') { greet(); return }   // resize/ping control frames
      const text = Buffer.from(msg).toString('utf8')
      // A DEL from an accepted completion must visibly erase, or the row the
      // component reads back would not match what the shell would show.
      ws.send(Buffer.from(text.replace(/\x7f/g, '\b \b'), 'utf8'))
    })
  })
}

async function openTerminal(page, base, theme) {
  await stubDashboardApi(page, { slots, theme: theme.base })
  logPageProblems(page)
  await stubPty(page)
  await page.route('**/api/terminal/complete', async route => {
    const body = JSON.parse(route.request().postData() || '{}')
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(answer(body)) })
  })
  await page.addInitScript(t => {
    localStorage.setItem('mc-color-theme', t)
    localStorage.setItem('mc-privacy-notice-v1', '1')
    localStorage.setItem('mc-nav', '1')
  }, theme.attr)

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    t => document.documentElement.getAttribute('data-theme') === t,
    theme.attr, { timeout: 15000 })

  await page.getByRole('button', { name: 'Terminal', exact: true })
    .and(page.locator('.nav-item'))
    .click()
  // The prompt arriving is the signal that xterm is live and the socket is up;
  // typing before it would land on an empty buffer with no command word.
  try {
    await page.waitForFunction(
      () => (document.querySelector('.xterm-rows')?.textContent || '').includes('❯'),
      null, { timeout: 15000 })
  } catch (e) {
    console.error('DEBUG xterm count:', await page.locator('.xterm-rows').count())
    console.error('DEBUG rows text:', JSON.stringify(
      (await page.locator('.xterm-rows').first().textContent().catch(() => '')).slice(0, 200)))
    console.error('DEBUG box:', JSON.stringify(await page.locator('.xterm').first().boundingBox().catch(() => null)))
    throw e
  }
  return page.locator('.xterm-screen')
}

/** Type into xterm's hidden textarea, then wait for the menu to settle. */
async function type(page, text) {
  await page.locator('.xterm-helper-textarea').first().type(text, { delay: 45 })
  await page.waitForTimeout(400)
}

async function menuShot(page, path) {
  const menu = page.getByTestId('terminal-completion')
  await menu.waitFor({ state: 'visible', timeout: 10000 })
  const rows = await menu.getByRole('option').count()
  const mode = await menu.getAttribute('data-mode')
  // Assert the SUBJECT, so a harness that silently stopped producing command
  // menus fails here instead of writing a misleading screenshot.
  if (mode !== 'command') throw new Error(`expected a command menu, got data-mode=${mode}`)
  if (rows === 0) throw new Error('command menu rendered with no rows')
  // Clip derived from the menu's own box rather than fixed coordinates: the menu
  // grows with the row count and flips above the cursor when it would not fit, so
  // a hardcoded rectangle silently crops the very thing being photographed.
  const box = await menu.boundingBox()
  const size = page.viewportSize()
  const top = Math.max(0, Math.round(box.y) - 90)   // include the typed command line
  const clip = {
    x: 0,
    y: top,
    width: size.width,
    height: Math.min(size.height - top, Math.round(box.height) + 120),
  }
  await page.screenshot({ path, clip })
  console.log(`wrote ${path} (${rows} rows, data-mode=${mode}, menu ${Math.round(box.height)}px)`)
}

const THEMES = [
  // `base` is what the stubbed config reports (light/dark); `attr` is the kiro
  // palette the boot effect writes to `data-theme`. Passing one where the other
  // is expected makes the theme assertion hang, not the capture fail loudly.
  { name: 'dark', base: 'dark', attr: 'kiro-dark' },
  { name: 'light', base: 'light', attr: 'kiro-light' },
]

/** One still per scene: what the menu looks like for each protocol shape. */
const SCENES = [
  { name: 'gh-pr-subcommands', keys: 'gh pr ', note: 'every subcommand, with the tool\'s own descriptions' },
  { name: 'gh-pr-narrowed', keys: 'gh pr c', note: 'narrowed by prefix, from the same cached listing' },
  { name: 'gh-pr-create-flags', keys: 'gh pr create --', note: 'flags — a word the path tier refuses outright' },
  { name: 'git-no-descriptions', keys: 'git c', note: 'git supplies no help text, so no description bar' },
]

/**
 * Move the docked terminal into a chat side-panel tab.
 *
 * The two surfaces render the same `CliPanel`, so this is not a second code path
 * being tested — it is the PROOF that there is only one. The move button carries
 * the live session across, so the same shell (and the same completion menu) has to
 * come with it.
 */
async function moveToSidePanel(page) {
  const move = page.getByRole('button', { name: /move to side panel/i })
  await move.waitFor({ state: 'visible', timeout: 10000 })
  await move.click()
  // The docked panel closes and the side panel mounts the same session; wait for
  // the prompt to reappear there rather than sleeping.
  await page.waitForFunction(
    () => {
      const rows = [...document.querySelectorAll('.xterm-rows')]
      return rows.length > 0 && rows.some(r => (r.textContent || '').includes('❯'))
    },
    null, { timeout: 15000 })
  await page.waitForTimeout(400)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  mkdirSync(OUT, { recursive: true })

  try {
    for (const theme of THEMES) {
      for (const scene of SCENES) {
        const context = await browser.newContext({
          viewport: { width: 1280, height: 860 }, deviceScaleFactor: 2,
        })
        const page = await context.newPage()
        await openTerminal(page, base, theme)
        await type(page, scene.keys)
        await menuShot(page, `${OUT}/${scene.name}-${theme.name}.png`)
        await context.close()
      }
    }

    // ── Surface parity: the same menu in the chat side panel ──
    {
      const context = await browser.newContext({
        viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2,
      })
      const page = await context.newPage()
      await openTerminal(page, base, THEMES[0])
      await moveToSidePanel(page)
      await type(page, 'gh pr ')
      await menuShot(page, `${OUT}/side-panel-parity-dark.png`)
      await context.close()
    }

    // ── The one frame the stills above cannot show: what ACCEPTING does ──
    // A repo-hosted webm cannot play inline in a PR body (GitHub only embeds
    // videos uploaded to its own asset host), so the sequence has to be legible
    // from stills alone. This is the missing beat: the subcommand is now on the
    // line and the menu has moved one level DOWN the tree rather than closing.
    {
      const context = await browser.newContext({
        viewport: { width: 1280, height: 860 }, deviceScaleFactor: 2,
      })
      const page = await context.newPage()
      await openTerminal(page, base, THEMES[0])
      await type(page, 'gh pr c')
      // checkout/checks/close/comment/create — walk to `create`.
      for (let i = 0; i < 4; i += 1) {
        await page.keyboard.press('ArrowDown')
        await page.waitForTimeout(120)
      }
      await page.keyboard.press('Enter')
      await page.waitForTimeout(700)
      const line = await page.locator('.xterm-rows').first().textContent()
      if (!line.includes('gh pr create')) {
        throw new Error(`acceptance did not reach the line: ${JSON.stringify(line)}`)
      }
      await type(page, '--')
      await menuShot(page, `${OUT}/after-accept-flags-dark.png`)
      await context.close()
    }

    // ── Video: the walk down a subcommand tree, which no still can show ──
    // Every step ASSERTS what it expects before moving on. A recording is only
    // evidence if it cannot silently record the wrong thing, and nobody reviews a
    // webm frame by frame — so the assertions are the proof and the video is the
    // illustration of it.
    //
    // The recording starts when the CONTEXT is created, so its first second or two
    // is the SPA booting on a blank page — useless as a GIF's first frame, which is
    // also its poster frame. The offset at which the terminal actually goes live is
    // therefore measured and printed, along with the terminal's own box, so the
    // webm→GIF conversion can trim and crop deterministically instead of by eye.
    const t0 = Date.now()
    const context = await browser.newContext({
      viewport: { width: 1280, height: 860 },
      recordVideo: { dir: OUT, size: { width: 1280, height: 860 } },
    })
    const page = await context.newPage()
    const menu = page.getByTestId('terminal-completion')
    const expectMenu = async (label, minRows) => {
      await menu.waitFor({ state: 'visible', timeout: 10000 })
      const rows = await menu.getByRole('option').count()
      const mode = await menu.getAttribute('data-mode')
      if (mode !== 'command') throw new Error(`${label}: data-mode=${mode}`)
      if (rows < minRows) throw new Error(`${label}: ${rows} rows, expected >= ${minRows}`)
      console.log(`  video step ok — ${label} (${rows} rows)`)
    }

    await openTerminal(page, base, THEMES[0])
    const liveAtMs = Date.now() - t0
    await page.waitForTimeout(800)
    await type(page, 'gh pr ')              // full subcommand list
    await expectMenu('gh pr — every subcommand', 16)
    await page.waitForTimeout(700)
    await type(page, 'c')                   // narrows in-place, no second request
    await expectMenu('gh pr c — narrowed', 5)
    await page.waitForTimeout(600)
    // Walk down to `create` (index 4 of checkout/checks/close/comment/create) —
    // the subcommand whose real flag output the fixtures hold, so the next step
    // has something true to show.
    for (let i = 0; i < 4; i += 1) {
      await page.keyboard.press('ArrowDown')
      await page.waitForTimeout(350)
    }
    await page.keyboard.press('Enter')      // accept -> re-opens one level down
    await page.waitForTimeout(900)
    // The accepted subcommand must be on the line: this is the insertion path
    // (DEL count + typed text) actually working against a live xterm.
    const line = await page.locator('.xterm-rows').first().textContent()
    if (!line.includes('gh pr create')) {
      throw new Error(`accepted subcommand missing from the line: ${JSON.stringify(line)}`)
    }
    await type(page, '--')                  // now the flags of that subcommand
    await expectMenu('gh pr create -- — flags', 10)
    await page.waitForTimeout(900)
    await page.keyboard.press('Escape')
    await menu.waitFor({ state: 'hidden', timeout: 5000 })
    await page.waitForTimeout(600)
    const video = page.video()
    const box = await page.locator('.xterm').first().boundingBox().catch(() => null)
    await context.close()
    if (video) {
      const src = await video.path()
      const dest = join(OUT, 'walk-the-subcommand-tree.webm')
      renameSync(src, dest)
      console.log(`wrote ${dest}`)
      // Deterministic inputs for the GIF the PR body embeds. Printed rather than
      // hardcoded because the boot time and the panel geometry both move.
      const crop = box
        ? `1280:${Math.min(860, Math.round(860 - box.y + 40))}:0:${Math.max(0, Math.round(box.y - 40))}`
        : '1280:400:0:460'
      console.log(`  gif trim: -ss ${(liveAtMs / 1000).toFixed(1)}   gif crop: ${crop}`)
      console.log(
        '  gif: ffmpeg -ss <trim> -i walk-the-subcommand-tree.webm '
        + `-vf "crop=${crop},fps=12,scale=900:-1:flags=lanczos,split[a][b];`
        + '[a]palettegen=max_colors=128:stats_mode=diff[p];'
        + '[b][p]paletteuse=dither=bayer:bayer_scale=3" -loop 0 walk-the-subcommand-tree.gif',
      )
    }
  } finally {
    await browser.close()
    srv.close()
  }
  console.log('\nfiles:', readdirSync(OUT).join(', '))
}

await main()
