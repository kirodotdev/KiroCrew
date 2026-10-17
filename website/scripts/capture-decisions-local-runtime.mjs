/**
 * Screenshot harness for the Decision model picker while the gateway prepares and
 * runs a local model (Settings > Developer > Feature Previews > Decisions).
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static server
 * and answers every /api/** call from fixtures, so no model is downloaded or run:
 * the states below are what `/api/decisions/provider` reports at each step of
 * `decisions/local_runtime.py`. Frames:
 *   decisions-local-not-downloaded-light.png  Laya picked, not yet saved: what the
 *                                             first use downloads.
 *   decisions-local-downloading-light.png     the download in progress.
 *   decisions-local-running-light.png         running, with Remove download offered
 *                                             for the model not in use.
 *   decisions-local-error-light.png           a model that could not start, with its
 *                                             log and Try again.
 *   decisions-local-installing-light.png      installing the model's software.
 *   decisions-local-starting-light.png        loading and starting the server.
 *   decisions-local-ready-light.png           a downloaded model picked, not in use.
 *   decisions-local-blocked-light.png         local models withdrawn by policy.
 *   decisions-local-running-switch-off-light.png  running while the Decisions switch
 *                                             is off: no "In use", and why.
 *   decisions-local-no-model-light.png        No model chosen: the switch held, the
 *                                             note in place of the egress line.
 *   decisions-local-card-light.png            the whole card: the switch, its
 *                                             description and the egress note above
 *                                             the picker, for a local model.
 *
 * Each frame asserts the text that makes it that state before it is written, so a
 * fixture typo cannot produce a tidy image of the wrong thing.
 *
 * Usage: node scripts/capture-decisions-local-runtime.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi, KIROCREW_CONFIG_FIXTURE } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/decisions-local-runtime'
mkdirSync(OUT, { recursive: true })

const PLUMB = {
  id: 'plumb-4b',
  name: 'Plumb-4B',
  model: 'plumb-4b',
  default_port: 8102,
  jev_relative_pct: 103,
  hard_relative_pct: 109,
  peak_ram_gb: 14.8,
  recommended_total_ram_gb: 60,
  p50_secs: 2.4,
  p95_secs: 32,
  timeout_ms: 5000,
  download_bytes: 8431572407,
  installed: false,
}
const LAYA = {
  ...PLUMB,
  id: 'laya',
  name: 'Laya',
  model: 'english',
  default_port: 8104,
  jev_relative_pct: 67,
  hard_relative_pct: 47,
  peak_ram_gb: 6,
  recommended_total_ram_gb: 24,
  p50_secs: 0.17,
  p95_secs: 0.51,
  timeout_ms: 2000,
  download_bytes: 846207419,
}
const STRANDS = {
  ...PLUMB,
  id: 'strands-decider-2b',
  name: 'Strands Decider 2B',
  model: 'strands-decider-2b',
  default_port: 8106,
  jev_relative_pct: 84,
  hard_relative_pct: 69,
  peak_ram_gb: 12,
  recommended_total_ram_gb: 48,
  p50_secs: 0.64,
  p95_secs: 11.7,
  timeout_ms: 5000,
  download_bytes: 4639855040,
}
const IDLE = { preset: '', state: 'idle', port: 0, bytes_done: 0, bytes_total: 0, error: '' }
const JEV_ENDPOINT = 'https://api.typesafe.ai/v1/systemone'

const provider = ({ active = 'jev', runtime = IDLE, presets = [PLUMB, STRANDS, LAYA], localPermitted = true, consentOn = true } = {}) => ({
  consentOn,
  presets,
  active,
  configured_endpoint:
    active === 'jev' ? JEV_ENDPOINT : active === 'none' ? 'none' : `http://127.0.0.1:${runtime.port || 8104}/v1/systemone`,
  loopback: active !== 'jev' && active !== 'none',
  hosted_permitted: true,
  local_permitted: localPermitted,
  runtime,
})

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const shot = []

  const openPage = async payload => {
    const context = await browser.newContext({ viewport: { width: 1360, height: 2200 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      theme: 'light',
      localStorageEntries: { 'mc-dev-mode': '1' },
      extra: async (path, route) => {
        if (path === '/api/decisions/provider') {
          await json(route, payload)
          return true
        }
        if (path === '/api/decisions/local-models/status') {
          if (payload.failStatus) {
            await route.fulfill({ status: 502, contentType: 'application/json', body: '{"error":"bad gateway"}' })
            return true
          }
          await json(route, {
            runtime: payload.runtime,
            installed: payload.presets.filter(p => p.installed).map(p => p.id),
          })
          return true
        }
        if (path.startsWith('/api/decisions/local-models/') && route.request().method() === 'DELETE') {
          await route.fulfill({
            status: payload.failDelete ? 500 : 200,
            contentType: 'application/json',
            body: payload.failDelete ? '{"error":"decisions_local_model_remove_failed"}' : '{"ok":true}',
          })
          return true
        }
        if (path === '/api/decisions/consent') {
          const on = payload.consentOn && payload.active !== 'none'
          await json(route, {
            enabled: on,
            endpoint: on ? payload.configured_endpoint : '',
            configured_endpoint: payload.configured_endpoint,
            permits: on,
            tool_args: false,
            compaction: false,
            history_budget_chars: 0,
            points: [],
          })
          return true
        }
        if (path === '/api/dashboard/config') {
          await json(route, { restore_sessions: false, decisions_enabled: true })
          return true
        }
        if (path === '/api/system') {
          await json(route, { mem_total_gb: 48 })
          return true
        }
        if (path === '/api/secrets') {
          await json(route, { names: [] })
          return true
        }
        if (path === '/api/config/kirocrew') {
          await json(route, {
            ...KIROCREW_CONFIG_FIXTURE,
            decisions: { bucket: 100, provider: { endpoint: payload.configured_endpoint } },
          })
          return true
        }
        return false
      },
    })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    const picker = page.getByRole('radiogroup', { name: 'Decision model' })
    await picker.waitFor({ state: 'visible', timeout: 15000 })
    await page.waitForTimeout(600)
    return { page, picker }
  }

  const save = async (picker, name) => {
    await picker.scrollIntoViewIfNeeded()
    const buf = await picker.screenshot({ path: `${OUT}/${name}.png` })
    shot.push(`${name}.png (${buf.readUInt32BE(16)}×${buf.readUInt32BE(20)})`)
  }

  const expectText = async (scope, pattern, what) => {
    await scope.getByText(pattern).first().waitFor({ state: 'visible', timeout: 5000 }).catch(() => {
      throw new Error(`${what} is not shown`)
    })
  }

  {
    const { page, picker } = await openPage(provider())
    await picker.getByRole('radio', { name: 'Laya' }).click()
    await expectText(picker, /downloads 0\.8\s*GB of model files and about 1 GB of software/, 'the download note')
    if ((await picker.getByRole('textbox').count()) > 0) throw new Error('a port field is still offered')
    await picker.getByRole('button', { name: 'Use this model' }).waitFor({ state: 'visible' })
    await save(picker, 'decisions-local-not-downloaded-light')
    await page.context().close()
  }
  {
    const { page, picker } = await openPage(
      provider({
        active: 'plumb-4b',
        runtime: { ...IDLE, preset: 'plumb-4b', state: 'downloading', port: 8102, bytes_done: 3.2e9, bytes_total: 8431572407 },
      }),
    )
    await expectText(picker, /Downloading the model: 3\.2\s*GB of 8\.4\s*GB/, 'the progress line')
    await picker.getByRole('progressbar', { name: 'Model download progress' }).waitFor({ state: 'visible' })
    await save(picker, 'decisions-local-downloading-light')
    await page.context().close()
  }
  {
    const { page, picker } = await openPage(
      provider({
        active: 'laya',
        runtime: { ...IDLE, preset: 'laya', state: 'running', port: 8104 },
        presets: [{ ...PLUMB, installed: true }, STRANDS, { ...LAYA, installed: true }],
      }),
    )
    await expectText(picker, 'Running on this machine.', 'the running line')
    await picker.getByRole('button', { name: /Remove Plumb-4B download/ }).waitFor({ state: 'visible' })
    if ((await picker.getByRole('button', { name: /Remove Laya download/ }).count()) > 0) {
      throw new Error('the model in use is offered for removal')
    }
    await save(picker, 'decisions-local-running-light')
    await page.context().close()
  }
  {
    const { page, picker } = await openPage(
      provider({
        active: 'plumb-4b',
        runtime: {
          ...IDLE,
          preset: 'plumb-4b',
          state: 'error',
          port: 8102,
          error:
            'the model server stopped 4 times in a row:\nLoading checkpoint shards...\nRuntimeError: [enforce fail at alloc_cpu.cpp:117] not enough memory: you tried to allocate 1048576000 bytes.',
        },
      }),
    )
    await expectText(picker, 'The model could not be started.', 'the error notice')
    await picker.getByRole('button', { name: 'Try again' }).waitFor({ state: 'visible' })
    await expectText(picker, /not enough memory/, 'the server log tail')
    await save(picker, 'decisions-local-error-light')
    await page.context().close()
  }
  {
    const { page, picker } = await openPage(
      provider({ active: 'laya', runtime: { ...IDLE, preset: 'laya', state: 'installing', port: 8104 } }),
    )
    await expectText(picker, /Installing the model's software/, 'the installing line')
    await expectText(picker, /Decisions are skipped until it is ready\. To stop/, 'the preparing note')
    await save(picker, 'decisions-local-installing-light')
    await page.context().close()
  }
  {
    const { page, picker } = await openPage(
      provider({
        active: 'laya',
        runtime: { ...IDLE, preset: 'laya', state: 'starting', port: 8104 },
        presets: [PLUMB, STRANDS, { ...LAYA, installed: true }],
      }),
    )
    await expectText(picker, /Starting the model\. Decisions are skipped until it is ready/, 'the starting line')
    await save(picker, 'decisions-local-starting-light')
    await page.context().close()
  }
  {
    const { page, picker } = await openPage(provider({ presets: [PLUMB, STRANDS, { ...LAYA, installed: true }] }))
    await picker.getByRole('radio', { name: 'Laya' }).click()
    await expectText(picker, /Downloaded\. .* starts it when you press Use this model/, 'the ready note')
    await save(picker, 'decisions-local-ready-light')
    await page.context().close()
  }
  {
    const { page, picker } = await openPage(provider({ localPermitted: false }))
    await expectText(picker, "Turned off by your organization's policy.", 'the policy note')
    if (!(await picker.getByRole('radio', { name: 'Laya' }).isDisabled())) throw new Error('a withdrawn model is choosable')
    await save(picker, 'decisions-local-blocked-light')
    await page.context().close()
  }
  {
    const { page } = await openPage(
      provider({
        active: 'laya',
        runtime: { ...IDLE, preset: 'laya', state: 'running', port: 8104 },
        presets: [PLUMB, STRANDS, { ...LAYA, installed: true }],
      }),
    )
    const card = page.locator('#decisions-egress-note').locator('xpath=..')
    await expectText(card, /A small, fast model makes quick calls/, 'the switch description')
    await expectText(card, /sent to the model server at the address below, on this machine/, 'the local egress note')
    await save(card, 'decisions-local-card-light')
    await page.context().close()
  }

  {
    const { page, picker } = await openPage(
      provider({
        active: 'laya',
        consentOn: false,
        runtime: { ...IDLE, preset: 'laya', state: 'running', port: 8104 },
        presets: [PLUMB, STRANDS, { ...LAYA, installed: true }],
      }),
    )
    await expectText(picker, /nothing is sent to it while the Decisions switch above is off/, 'the switch-off line')
    if (/In use/.test(await picker.getByText('Laya', { exact: true }).locator('xpath=ancestor::label').innerText())) {
      throw new Error('a model is marked In use under an off switch')
    }
    await save(picker, 'decisions-local-running-switch-off-light')
    await page.context().close()
  }
  {
    const { page, picker } = await openPage({
      ...provider({
        active: 'plumb-4b',
        runtime: { ...IDLE, preset: 'plumb-4b', state: 'downloading', bytes_done: 2.1e9, bytes_total: 8.43e9 },
      }),
      failStatus: true,
    })
    await expectText(picker, /Lost contact with the gateway; this progress may be out of date/, 'the poll-failure notice')
    await save(picker, 'decisions-local-poll-failed-light')
    await page.context().close()
  }
  {
    const { page, picker } = await openPage({
      ...provider({
        active: 'laya',
        runtime: { ...IDLE, preset: 'laya', state: 'running', port: 8104 },
        presets: [{ ...PLUMB, installed: true }, STRANDS, { ...LAYA, installed: true }],
      }),
      failDelete: true,
    })
    await picker.getByRole('button', { name: /Remove Plumb-4B download/ }).click()
    await expectText(picker, /Could not remove the download/, 'the remove-failure notice')
    await save(picker, 'decisions-local-remove-failed-light')
    await page.context().close()
  }
  {
    const { page } = await openPage(provider({ active: 'none', presets: [PLUMB, STRANDS, { ...LAYA, installed: true }] }))
    const card = page.locator('#decisions-egress-note').locator('xpath=../..')
    await expectText(card, /No decision model is chosen\. Pick one below/, 'the no-model note')
    await expectText(card, /Nothing is sent and no model runs/, 'the No model option')
    if ((await card.getByText(/sent over the internet/).count()) > 0) throw new Error('the egress line is still shown')
    await save(card, 'decisions-local-no-model-light')
    await page.context().close()
  }

  await browser.close()
  srv.close()
  console.log(`wrote ${shot.length} shot(s) to ${OUT}: ${shot.join(', ')}`)
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
