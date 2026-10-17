/**
 * Screenshot harness for the FEISHU (飞书/Lark) CHANNEL settings panel.
 *
 * The panel is the whole point of the change: the channel's backend already
 * worked, but the only way to configure it was hand-editing `.env` and
 * `config.json`, and it did not appear in Settings -> Channels at all. So the
 * shots that matter are the ones proving it is now reachable AND that the states
 * a user actually lands in read correctly.
 *
 * Five scenarios, each against the real built SPA (website/dist) with the
 * dashboard API stubbed:
 *
 *   list          — the channel list itself, which is where Feishu was missing
 *   needs-setup   — first run: no credentials, so the guide card is the content
 *   missing-extra — credentialed but `lark-oapi` is not installed. This is the
 *                   failure a user cannot diagnose from a bare "not connected",
 *                   so the panel shows the install command for THIS gateway's
 *                   interpreter — installing into a different environment is the
 *                   actual failure mode, and a bare `pip` cannot say which.
 *   sdk-unsupported — same missing SDK, but in an environment where no pip
 *                   install can work (bundled desktop app, no pip, PEP 668).
 *                   A command there would be discarded on the next app update,
 *                   so the card must withhold it and say so.
 *   connected     — running, with both allow-lists populated
 *   group-empty   — group chats ON with an empty chat_id list. Both fail closed,
 *                   so that combination serves NO group; the warning is the
 *                   feature, because without it the panel looks configured while
 *                   doing nothing.
 *
 * `missing-extra` and `group-empty` are the two that justify their own shots:
 * neither is expressible as a layout variant, and both are states where a panel
 * that said nothing would be actively misleading.
 *
 * ## Why some scenarios take a SECOND shot
 *
 * The settings detail pane is its own scroll container, so Playwright's
 * `fullPage` does NOT lengthen the image to include what is below its fold — the
 * first version of this harness photographed the same above-the-fold region for
 * `connected` and `group-empty` and produced two BYTE-IDENTICAL files, i.e. zero
 * evidence for the section the change actually adds. Scenarios that own a
 * below-the-fold surface therefore scroll it into view and take a `-group` shot,
 * and assert their distinguishing text is visible first so a silent regression
 * cannot pass as a screenshot.
 *
 * Nothing in CI runs this file; it exists so the PR's evidence is the same
 * evidence used to verify the change. Both themes are captured because the
 * status badge and the guide card carry their own surfaces.
 *
 * Usage: node scripts/capture-feishu-panel.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/feishu-panel'

const OPEN_ID = 'ou_c99cbd8a1b2c3d4e5f6a7b8c9d0e1f2a'
const CHAT_ID = 'oc_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6'

/** Everything GET /api/feishu/config returns, in its unconfigured shape. */
const BASE_CONFIG = {
  connected: false,
  connect_error: '',
  configured: false,
  read_only: false,
  bot_token_set: false,
  bot_token_preview: '',
  bot_id_set: false,
  bot_id_preview: '',
  enabled: false,
  allowed_user_ids: [],
  allow_group: false,
  allowed_group_ids: [],
  soft_threshold_pct: 80,
  session_folder: '',
  // The optional [feishu] extra is present in every shot except the two that are
  // ABOUT it missing: a stub that omitted these would render the install card on
  // every scenario and photograph a state no configured user is in.
  sdk_installed: true,
  sdk_install_supported: true,
  sdk_install_command: '',
}

/** Credentialed + enabled, which is what every non-first-run shot starts from. */
const CREDENTIALED = {
  ...BASE_CONFIG,
  configured: true,
  enabled: true,
  bot_token_set: true,
  bot_token_preview: 'AbC…89cd',
  bot_id_set: true,
  bot_id_preview: 'cli…6g7h',
  allowed_user_ids: [OPEN_ID],
}

const SCENARIOS = [
  // Stays on the list rather than deep-linking into the panel: the complaint was
  // that Feishu never showed up HERE, so the row itself is the evidence.
  { name: 'list', config: BASE_CONFIG, stayOnList: true },
  // The guide card spells the install command out in prose, so it carries a
  // SECOND copy of the pin (one per locale) that no Python caller reads.
  // Assert it here too, or a stale hand-written pin ships beside the
  // generated one and the panel shows two different commands.
  {
    name: 'needs-setup',
    config: BASE_CONFIG,
    expectText: /pip install "lark-oapi>=1\.4,<2"/,
  },
  {
    name: 'missing-extra',
    config: {
      ...CREDENTIALED,
      connect_error: "lark-oapi is not installed — run: pip install 'lark-oapi>=1.4,<2'",
      sdk_installed: false,
      sdk_install_supported: true,
      sdk_install_command: "/opt/kirocrew/.venv/bin/python -m pip install 'lark-oapi>=1.4,<2'",
    },
    // Assert the COMMAND, not the badge reason: the badge cannot report this
    // state before a restart (maybe_start_feishu returns early when the channel
    // is disabled, ahead of the ImportError branch that writes the reason), so
    // the card is the surface under test.
    expectText: /-m pip install 'lark-oapi>=1\.4,<2'/,
  },
  {
    name: 'sdk-unsupported',
    config: {
      ...CREDENTIALED,
      sdk_installed: false,
      sdk_install_supported: false,
      sdk_install_command: '',
    },
    expectText: /cannot install extra packages/,
  },
  {
    name: 'connected',
    config: {
      ...CREDENTIALED,
      connected: true,
      allow_group: true,
      allowed_group_ids: [CHAT_ID],
      session_folder: 'Feishu',
    },
    // A listed chat_id and no warning: the configured group case.
    groupShot: { expectText: new RegExp(CHAT_ID) },
  },
  // allow_group with an empty list: on, and serving nothing.
  {
    name: 'group-empty',
    config: { ...CREDENTIALED, connected: true, allow_group: true },
    groupShot: { expectText: /turn the toggle off/ },
  },
]

function routes(scenario) {
  return async (path, route) => {
    if (path === '/api/feishu/config') return json(route, scenario.config), true
    // The channel accordion hides EVERY channel's settings behind the effective
    // `channels` governance policy, and a failed read is treated as "cannot
    // confirm" rather than as permitted. Without this the pane renders the
    // policy-unavailable placeholder and the screenshot shows no panel at all.
    if (path === '/api/governance/channels') return json(route, { feishu: true }), true
    return false
  }
}

async function main() {
  mkdirSync(OUT, { recursive: true })
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    for (const theme of ['light', 'dark']) {
      for (const scenario of SCENARIOS) {
        const context = await browser.newContext({
          viewport: { width: 1400, height: 1000 },
          deviceScaleFactor: 2, // 12-13px type renders soft at 1x on GitHub
        })
        const page = await context.newPage()
        logPageProblems(page)
        await stubDashboardApi(page, { theme, extra: routes(scenario) })
        // Selection is URL-backed (?channel=<key>), so deep-link straight to the
        // panel rather than clicking a row: the list renders role="option" in a
        // listbox, and on a narrow viewport the list and the detail pane swap.
        const url = scenario.stayOnList
          ? '/settings?tab=channels'
          : '/settings?tab=channels&channel=feishu'
        await page.goto(base + url, { waitUntil: 'domcontentloaded' })
        await page.getByText(/Feishu/).first().waitFor({ state: 'visible', timeout: 20000 })
        await page.waitForTimeout(700) // query settle

        if (scenario.expectText) {
          await page
            .getByText(scenario.expectText)
            .first()
            .waitFor({ state: 'visible', timeout: 15000 })
        }

        const file = `${OUT}/feishu-${scenario.name}-${theme}.png`
        await page.screenshot({ path: file, fullPage: true })
        console.log('wrote', file)

        if (scenario.groupShot) {
          // The group card is below the detail pane's own fold. Scroll it into
          // view and assert the distinguishing text BEFORE shooting, so a shot
          // can never be produced for a section that did not render.
          const toggle = page.getByRole('switch', { name: /Answer in group chats/ }).first()
          await toggle.waitFor({ state: 'visible', timeout: 15000 })
          await toggle.scrollIntoViewIfNeeded()
          await page.waitForTimeout(400)
          await page
            .getByText(scenario.groupShot.expectText)
            .first()
            .waitFor({ state: 'visible', timeout: 15000 })
          const gfile = `${OUT}/feishu-${scenario.name}-group-${theme}.png`
          await page.screenshot({ path: gfile, fullPage: true })
          console.log('wrote', gfile)
        }

        await context.close()
      }
    }
  } finally {
    await browser.close()
    srv.close()
  }
}

main()
