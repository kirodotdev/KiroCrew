/** Real-SPA geometry/scroll regression, using only synthetic provider data.
 * node scripts/capture-artifacts-provider-scroll.mjs <output-directory>
 * EXPECT_BROKEN=1 captures the base revision without asserting the repair.
 */
import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
import { chromium } from 'playwright'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const output = process.argv[2]
assert(output, 'Pass an output directory')
mkdirSync(output, { recursive: true })
const broken = process.env.EXPECT_BROKEN === '1'
const stamp = '2026-09-16T07:00:00Z'
const providers = ['Notebook', 'Canvas'].map(display_name => ({
  name: display_name.toLowerCase(), display_name, capable: true,
  available: true, kind_support: 'native', capabilities: [],
  discovery_model: { list_mine: true, list_shared_with_me: false,
    list_public: false, full_text_search: false, pull_by_id: true },
}))
const artifacts = Array.from({ length: 36 }, (_, i) => ({
  slug: `saved-${i}`, name: `Saved note ${i}`, kind: 'markdown', source: 'chat',
  description: 'Saved library fixture', tags: [], version: 1, pinned: false,
  created_at: stamp, updated_at: stamp,
}))
const docs = Array.from({ length: 40 }, (_, i) => ({
  path: `/workspace/notes/chat-${i}.md`, name: `Chat note ${i}`,
  updated_at: stamp, session_key: 'fixture', session_title: 'Test chat',
  message_ts: stamp, saved: false, slug: '',
}))
let count = 36
const extra = async (path, route) => {
  if (path === '/api/artifacts') return json(route, { artifacts: artifacts.slice(0, count) }), true
  if (path === '/api/artifact-folders') return json(route, { folders: [] }), true
  if (path === '/api/artifacts/session-docs') return json(route, { docs }), true
  if (path === '/api/artifacts/publish-providers') return json(route, { providers }), true
  const remote = /^\/api\/remote-artifacts\/([^/]+)\/browse$/.exec(path)
  if (remote) return json(route, { artifacts: Array.from({ length: 20 }, (_, i) => ({
    external_id: `${remote[1]}-${i}`, title: `${remote[1]} document ${i}`,
    owner: 'test-user', updated_at: stamp, local_slug: null,
  })) }), true
  const local = /^\/api\/artifacts\/(saved-\d+)$/.exec(path)
  if (local) return json(route, { ...artifacts.find(a => a.slug === local[1]), content: '# Saved note\n\nFixture text.' }), true
  return false
}
const { srv, base } = await serveDist()
const browser = await chromium.launch({ ignoreDefaultArgs: ['--hide-scrollbars'] })
const results = []
try {
  const cases = [[1440, 900, 36], [1024, 500, 36], [740, 500, 36], [390, 844, 36], [320, 640, 36], [1440, 900, 4]]
  for (const [width, height, n] of cases) {
    count = n
    const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 1 })
    const page = await context.newPage()
    await stubDashboardApi(page, { extra, theme: 'dark', localStorageEntries: {
      'mc-artifacts-view': 'grid', 'mc-artifacts-pinned-only': '0',
    } })
    await page.goto(`${base}/artifacts`)
    await page.getByText('On Canvas', { exact: true }).waitFor()
    await page.waitForTimeout(800)
    const host = page.getByTestId('artifacts-scroll-host')
    const metrics = await host.evaluate(el => ({
      overflow: getComputedStyle(el).overflowY,
      client: el.clientHeight, scroll: el.scrollHeight,
    }))
    results.push({ width, height, count: n, ...metrics })
    await page.screenshot({ path: `${output}/${width}-${height}-${n}-top.png` })
    if (!broken) {
      assert.equal(metrics.overflow, 'auto', 'Provider-enabled page must scroll')
      const toolbar = await page.getByRole('heading', { name: 'Your Artifacts', exact: true }).boundingBox()
      const chats = await page.getByRole('button', { name: 'From your chats', exact: true }).boundingBox()
      assert(toolbar && chats && toolbar.y + toolbar.height <= chats.y, 'Toolbar must not overlap chat documents')
      if (width >= 740 && n >= 30) {
        assert((await page.getByTestId('artifacts-gallery').boundingBox()).height >= 250, 'Saved gallery must not collapse')
      }
      await page.getByRole('button', { name: 'Show all (40)' }).click()
      for (const name of ['Chat note 39', 'notebook document 19', 'canvas document 19']) {
        const row = page.getByText(name, { exact: true })
        await row.scrollIntoViewIfNeeded()
        const box = await row.boundingBox()
        assert(box && box.y >= 0 && box.y + box.height <= height, `${name} must reach the viewport`)
      }
      const notebook = await page.getByText('On Notebook', { exact: true }).locator('..').boundingBox()
      const canvas = await page.getByText('On Canvas', { exact: true }).locator('..').boundingBox()
      assert(notebook.y + notebook.height <= canvas.y, 'Provider cards must not overlap')
      const position = await host.evaluate(el => el.scrollTop)
      await page.getByText('canvas document 19', { exact: true }).hover()
      await page.mouse.wheel(0, -200)
      await page.waitForTimeout(200)
      assert(await host.evaluate(el => el.scrollTop) < position, 'Wheel over provider rows must scroll the page')
      await page.getByText('canvas document 19', { exact: true }).scrollIntoViewIfNeeded()
      await page.screenshot({ path: `${output}/${width}-${height}-${n}-bottom.png` })
    }
    console.log(JSON.stringify(results.at(-1)))
    await context.close()
  }
} finally {
  await browser.close()
  await new Promise(resolve_ => srv.close(resolve_))
  writeFileSync(`${output}/results.json`, JSON.stringify(results, null, 2))
}
