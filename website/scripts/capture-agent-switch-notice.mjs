/* Screenshots for the agent-switch refusal notice.
 *
 * Drives website/capture/agent-switch-workspace-unavailable.html, which mounts the REAL
 * `AgentSwitchNotice` with copy resolved through the REAL `agentSwitchFailureMessage`.
 *
 * Each frame asserts the notice's own text before writing, so a frame is never committed as
 * evidence of copy it does not show. The two cases share one surface and are told apart only
 * by the error code, so each asserts the other's copy is ABSENT.
 *
 *   01-workspace-unavailable   the 503 this change added
 *   02-turn-in-flight          the sibling 409, so the frames prove which copy is which
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort   # in another shell
 *   node scripts/capture-agent-switch-notice.mjs http://127.0.0.1:6841 ../temp-screenshots/agent-switch-notice
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/agent-switch-notice'
mkdirSync(OUT, { recursive: true })

let failed = false
function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

const SCENES = [
  {
    file: '01-workspace-unavailable',
    case: 'workspace',
    mustCarry: ["project folder isn't available", 'check that it exists'],
    mustNotCarry: ['turn is in flight', 'the configured workspace directory is unavailable'],
  },
  {
    file: '02-turn-in-flight',
    case: 'turn',
    mustCarry: ['turn'],
    mustNotCarry: ["workspace folder isn't available"],
  },
]

const browser = await chromium.launch()

for (const theme of ['dark', 'light']) {
  for (const scene of SCENES) {
    const page = await browser.newPage({ viewport: { width: 760, height: 150 }, deviceScaleFactor: 2 })
    await page.goto(
      `${BASE}/capture/agent-switch-workspace-unavailable.html?theme=${theme}&case=${scene.case}`,
    )
    await page.waitForSelector('[data-capture-root]')
    const notice = page.getByTestId('agent-switch-notice')
    await notice.waitFor({ timeout: 5000 }).catch(() => {})
    const shown = (await notice.count()) === 1
    const text = shown ? (await notice.textContent()) || '' : ''
    const carried = scene.mustCarry.every(s => text.includes(s))
    // The backend's English prose must not reach this surface: that is the whole reason the
    // helper maps this code instead of letting the generic path prefer the API message.
    const leaked = scene.mustNotCarry.filter(s => text.includes(s))

    const name = `${scene.file}-${theme}`
    if (
      check(
        name,
        shown && carried && leaked.length === 0,
        `shown=${shown} carried=${carried} leaked=${JSON.stringify(leaked)} text=${JSON.stringify(text.slice(0, 120))}`,
      )
    ) {
      await page.screenshot({ path: `${OUT}/${name}.png` })
    }
    await page.close()
  }
}

await browser.close()
process.exit(failed ? 1 : 0)
