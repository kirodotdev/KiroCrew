/**
 * Screenshot harness for the Dev Container trust prompt.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth, no Docker.
 *
 * Shoots three states, because the card's whole job is informed consent and no
 * single frame shows that:
 *   1. `prompt`    — the untrusted config as the user first meets it, with the
 *                    raw text collapsed. This is the default state, so it is
 *                    what most users actually see.
 *   2. `expanded`  — the raw config revealed. Trust is granted against exact
 *                    bytes, so a reviewer needs to see that the bytes really are
 *                    on screen and legible rather than summarized.
 *   3. `inputs`    — a config whose build reads more of `.devcontainer/` than the
 *                    json alone, which the prompt has to disclose: the grant
 *                    covers the whole tree, not just the file being read.
 *
 * The feature is off behind two locks in production; the fixture reports
 * `enabled: true` because these frames are about the card, not the gate.
 *
 * Usage: node scripts/capture-devcontainer-trust.mjs [outDir] [prefix]
 *   On a main build the card does not exist, so `before` shoots a plain composer.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/devcontainer-trust'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const PROJECT = '/home/dev/work/payments-api'
const CONFIG_PATH = `${PROJECT}/.devcontainer/devcontainer.json`
const DIGEST = 'b7c1e9f4a2d85630bb41c7f0e39a5d8c62471fe0a9b3c5d7e8f1a2b3c4d5e6f7'

// A realistic config: a compose-based dev container that asks for a bind and a
// lifecycle hook, i.e. exactly the sort of thing the prompt exists to surface.
const RAW = `{
  "name": "payments-api",
  "dockerComposeFile": "docker-compose.yml",
  "service": "app",
  "workspaceFolder": "/workspaces/payments-api",
  "remoteUser": "vscode",
  "features": {
    "ghcr.io/devcontainers/features/docker-outside-of-docker:1": {}
  },
  "mounts": [
    "source=\${localEnv:HOME}/.cache/pip,target=/home/vscode/.cache/pip,type=bind"
  ],
  "postCreateCommand": "./scripts/dev-setup.sh && pip install -e .",
  "runArgs": ["--memory=4g"]
}
`

const STATUS = {
  project_dir: PROJECT,
  enabled: true,
  has_config: true,
  config_path: CONFIG_PATH,
  trusted: false,
  container_id: null,
  running: false,
  remote_workspace_folder: null,
}

const CONFIG = {
  config_path: CONFIG_PATH,
  digest: DIGEST,
  raw: RAW,
  name: 'payments-api',
  image: null,
  trusted: false,
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 1000 },
    deviceScaleFactor: 2, // 12-13px type renders soft at 1x on GitHub
  })
  const page = await context.newPage()
  logPageProblems(page)

  // `otherInputs` varies per shot, so the route handler reads it from here.
  let otherInputs = []

  await stubDashboardApi(page, {
    slots: [{
      key: 's1', title: 'payments-api', messages: 0, running: false,
      agent: 'kirocrew', mode: '', created: '2026-08-01T01:00:00Z',
      last_ts: '2026-08-11T12:00:00Z', folder_id: '', project: PROJECT,
    }],
    extra: async (path, route) => {
      // Must `await` then return true: the stub's contract is
      // `if (await extra(...)) return`, and `route.fulfill()` resolves to
      // undefined, so returning it directly reads as unhandled and the stub
      // fulfils a second time ("Route is already handled!").
      if (path.startsWith('/api/devcontainer/status')) {
        await json(route, STATUS)
        return true
      }
      if (path.startsWith('/api/devcontainer/config')) {
        await json(route, { ...CONFIG, other_inputs: otherInputs })
        return true
      }
      return false
    },
  })

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })

  // The component carries no testid, so anchor on the heading the prompt is
  // built around and climb to the card container.
  const heading = page.getByText(
    /run this project's agent inside its dev container\\?/i,
  ).first()
  const card = page.locator('section, [class*="rounded"]')
    .filter({ has: page.getByRole('button', { name: /^trust$/i }) }).last()
  await heading.waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(500) // let the config fetch settle before shooting

  const shot = []
  const save = async (name, locator) => {
    const path = `${OUT}/${PREFIX}-${name}.png`
    await (locator || page).screenshot({ path })
    shot.push(path)
  }

  // 1. As first met: raw text collapsed.
  await save('prompt', card)

  // 2. Raw config revealed — the bytes trust is granted against.
  const reveal = card.getByRole('button', { name: /show|view|raw|config/i }).first()
  if (await reveal.count()) {
    await reveal.click()
    await page.waitForTimeout(350)
    await save('expanded', card)
  }

  // 3. A build that reads more of the tree than the json alone. The list lives
  //    INSIDE the disclosure, so this shot has to expand it — collapsed, the
  //    frame is byte-identical to shot 1 and would claim something it does not
  //    show.
  otherInputs = ['Dockerfile', 'docker-compose.yml', 'scripts/dev-setup.sh']
  await page.reload({ waitUntil: 'domcontentloaded' })
  await heading.waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(600)
  const reveal2 = card.getByRole('button', { name: /show|view|raw|config|details/i }).first()
  if (!(await reveal2.count())) throw new Error('disclosure control not found for shot 3')
  await reveal2.click()
  await page.waitForTimeout(400)
  const inputsList = card.getByText(/dev-setup\.sh/).first()
  await inputsList.waitFor({ state: 'visible', timeout: 10000 })
  await save('inputs', card)

  await browser.close()
  srv.close()
  for (const p of shot) console.log(p)
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
