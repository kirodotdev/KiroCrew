/**
 * Screenshot harness: drag a Files-tab tree row into the composer.
 *
 * The REAL built SPA behind the shared static server, every `/api/**` answered
 * from fixtures (the `capture-pierre-files-tab.mjs` pattern). A real Chromium
 * mouse drag carries `README.md` and then the `docs` folder out of the tree's
 * shadow root onto the composer.
 *
 * Frames (`<out>/<prefix>-NN-*.png`):
 *   01-dragging   a row held over the composer: the dashed drop outline shows
 *   02-dropped    after the drop: `@README.md ` in the text and its chip staged
 *   03-folder     after dropping the `docs/` folder: `@docs/ ` appended
 *   04-refused    the `My Docs/` folder held over the composer: no folder
 *                 reference can carry its space, so the composer says so
 *   05-menu       right-click on `My Docs/`: Add to chat is disabled with the
 *                 same reason
 *   06-drop-point `package.json` held at the start of the draft's second line:
 *                 an insertion caret marks where the release will land
 *   07-placed     after that drop: the mention sits where it was let go, not
 *                 at the caret (the end of the draft)
 *
 * Usage: node scripts/capture-file-tree-drag-mention.mjs <out-dir> [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/file-tree-drag-mention'
const PREFIX = process.argv[3] || 'after'
const PROJECT = '/home/dev/project'
const SLOT = 'chat-tree-drag'

mkdirSync(OUT, { recursive: true })

const TREE_PATHS = ['README.md', 'package.json', 'docs/guide.md', 'docs/setup.md', 'My Docs/notes.md', 'src/app.ts', 'src/util.ts']

const slots = [{
  key: SLOT, title: 'Tree drag', running: false, last_message: 'hi', messages: 2,
  agent: 'kirocrew', memory_mode: 'persistent', project: PROJECT,
  modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
}]
const t0 = Math.floor(Date.now() / 1000) - 600
const slotDetail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', content: 'What does this project do?', ts: String(t0) },
    { role: 'assistant', content: 'It is a small web app. Point me at the files you want explained.', ts: String(t0 + 30) },
  ],
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch({ executablePath: chromiumExecutable() })
  const context = await browser.newContext({ viewport: { width: 1280, height: 760 }, deviceScaleFactor: 1 })
  const page = await context.newPage()

  const extra = async (path, route) => {
    if (path === '/api/chat/slots') return json(route, slots), true
    if (/^\/api\/chat\/slots\/[^/]+/.test(path)) return json(route, slotDetail), true
    if (path === '/api/project/tree') return json(route, { root: PROJECT, paths: TREE_PATHS, repo: false, truncated: false }), true
    if (path === '/api/project/git/status') return json(route, { repo: false, files: [] }), true
    if (path === '/api/project/git') return json(route, { path: PROJECT, repo: false }), true
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] }), true
    return false
  }
  await stubDashboardApi(page, { slots, extra })
  logPageProblems(page)

  await page.addInitScript(([slot, project]) => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot-chat', slot)
    localStorage.setItem('mc-activity-open:' + slot, 'true')
    localStorage.setItem('mc-panel-tabs:' + slot, JSON.stringify({ activeId: 'files', tabs: [{ id: 'files', kind: 'files', title: 'Files' }] }))
    localStorage.setItem('mc-files-rail-open', '1')
    localStorage.setItem('mc-files-rail-w', '300')
    localStorage.setItem('mc-side-panel-width', '520')
    localStorage.setItem('mc-git-panel-opened:' + slot + ':' + project, '1')
    localStorage.setItem('mc-chat-config', JSON.stringify({ pinLastPrompt: false, streamMode: 'immediate' }))
  }, [SLOT, PROJECT])
  await page.goto(base + '/?sid=' + encodeURIComponent(SLOT), { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(() => (document.querySelector('file-tree-container')?.shadowRoot?.textContent ?? '').includes('README'), null, { timeout: 20000 })
  await page.waitForTimeout(800)

  const row = path => page.locator('file-tree-container').locator(`[data-type="item"][data-item-path="${path}"]`)
  const textarea = page.locator('textarea[data-composer-input]').first()
  const wrapper = page.getByTestId('input-wrapper').first()

  const drag = async (path, { holdFrame, expect = 'accept', at } = {}) => {
    const from = await row(path).boundingBox()
    const to = await textarea.boundingBox()
    if (!from || !to) throw new Error(`no geometry for ${path}`)
    const target = at ?? { x: to.x + to.width / 2 + 4, y: to.y + to.height / 2 }
    await page.mouse.move(from.x + 20, from.y + from.height / 2)
    await page.mouse.down()
    await page.mouse.move(from.x + 40, from.y + from.height / 2, { steps: 4 })
    await page.mouse.move(target.x - 4, target.y, { steps: 12 })
    // Chromium fires `dragover` on pointer movement; nudge so the composer gets one.
    await page.mouse.move(target.x, target.y, { steps: 2 })
    await page.waitForTimeout(250)
    if (holdFrame) {
      const attr = expect === 'refuse' ? 'data-tree-drop-refused' : 'data-tree-drop-active'
      if ((await wrapper.getAttribute(attr)) !== 'true') throw new Error(`composer did not show the ${expect} state while hovering`)
      if (at) {
        const caret = await page.getByTestId('composer-tree-drop-caret').boundingBox()
        if (!caret || Math.abs(caret.x - at.x) > 12 || caret.y > at.y || caret.y + caret.height < at.y) {
          throw new Error(`drop caret ${JSON.stringify(caret)} is not at the pointer ${JSON.stringify(at)}`)
        }
      }
      await page.screenshot({ path: `${OUT}/${PREFIX}-${holdFrame}.png` })
    }
    await page.mouse.up()
    await page.waitForTimeout(400)
  }

  await drag('README.md', { holdFrame: '01-dragging' })
  const afterFile = await textarea.inputValue()
  if (afterFile !== '@README.md ') throw new Error(`file drop inserted ${JSON.stringify(afterFile)}`)
  await page.screenshot({ path: `${OUT}/${PREFIX}-02-dropped.png` })

  await drag('docs/')
  const afterDir = await textarea.inputValue()
  if (afterDir !== '@README.md @docs/ ') throw new Error(`folder drop left ${JSON.stringify(afterDir)}`)
  await page.screenshot({ path: `${OUT}/${PREFIX}-03-folder.png` })
  console.log('composer text:', JSON.stringify(afterDir))

  await drag('My Docs/', { holdFrame: '04-refused', expect: 'refuse' })
  const afterRefused = await textarea.inputValue()
  if (afterRefused !== afterDir) throw new Error(`refused folder changed the text to ${JSON.stringify(afterRefused)}`)

  await row('My Docs/').click({ button: 'right' })
  const refusedRow = page.getByTestId('file-tree-add-to-chat-refused')
  await refusedRow.waitFor({ state: 'visible', timeout: 5000 })
  if ((await refusedRow.getAttribute('aria-disabled')) !== 'true') throw new Error('refused Add to chat row is not disabled')
  await page.waitForTimeout(200)
  await page.screenshot({ path: `${OUT}/${PREFIX}-05-menu.png` })
  await page.keyboard.press('Escape')

  // Drop point: a two-line draft with the caret left at its end; the row is
  // let go at the start of the second line, and the mention must land there.
  await textarea.fill('first line\nsecond line')
  await textarea.press('End')
  await page.waitForTimeout(200)
  const line2 = await textarea.evaluate(el => {
    const r = el.getBoundingClientRect()
    const cs = getComputedStyle(el)
    const lh = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.4
    return {
      x: r.left + (parseFloat(cs.borderLeftWidth) || 0) + (parseFloat(cs.paddingLeft) || 0) + 1,
      y: r.top + (parseFloat(cs.borderTopWidth) || 0) + (parseFloat(cs.paddingTop) || 0) + lh * 1.5 - el.scrollTop,
    }
  })
  await drag('package.json', { holdFrame: '06-drop-point', at: line2 })
  const placed = await textarea.inputValue()
  if (placed !== 'first line\n@package.json second line') throw new Error(`drop-point insert left ${JSON.stringify(placed)}`)
  await page.screenshot({ path: `${OUT}/${PREFIX}-07-placed.png` })
  console.log('drop-point text:', JSON.stringify(placed))

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
