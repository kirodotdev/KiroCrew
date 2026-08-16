/**
 * Screenshot harness for the file panel's ⋯ menu after restoring the two
 * desktop hand-off entries ("Open with default app" / "Show in file manager").
 *
 * The menu IS the change, so the evidence has to be the menu OPEN at a crop
 * where the 13px labels are legible, in both themes — plus one non-English
 * locale, because the restored entries needed three new catalog keys and a
 * missing translation is invisible in an English-only shot.
 *
 * The harness also ASSERTS the item list rather than only photographing it: a
 * capture that silently lost an entry would still produce a plausible picture,
 * which is exactly how these two got deleted in the first place.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures (gateway-free — no
 * kiro-cli, no credentials). Only the network and the localStorage seed are
 * stubbed; the client code under test is unmodified.
 *
 * Usage: node scripts/capture-panel-reveal-menu.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/panel-reveal-menu'
const SLOT = 'panel-reveal-menu'
const DOC_PATH = '/home/builder/project/docs/release-notes.md'
const DOC = [
  '# Release notes',
  '',
  'The side panel can hand a file back to the desktop again: open it in the',
  'default application, or select it in the system file manager.',
  '',
  '- Headless hosts have neither, so the path goes to the clipboard instead.',
  '- The backend endpoint never went away; only the two menu entries did.',
  '',
].join('\n')

/** The ⋯ menu's FULL inventory in DOM order, per locale.
 *
 * Widened from the two restored labels to the whole list for the reason #1083
 * exists: an assertion that names only the entries someone thought to name
 * cannot catch the disappearance of one nobody named. `MarkdownPanel.test.tsx`
 * holds the same inventory at the unit level; this is the rendered-DOM copy,
 * which additionally proves the catalog keys resolve rather than falling back
 * to raw key strings.
 */
const EXPECTED = {
  en: [
    'Refresh', 'Full screen', 'Add to artifacts', 'Add to Knowledge',
    'Open with default app', 'Show in file manager',
    'Copy path', 'Copy content', 'Download',
  ],
  'zh-CN': [
    '刷新', '全屏', '添加到工件', '添加到知识库',
    '用默认应用打开', '在文件管理器中显示',
    '复制路径', '复制内容', '下载',
  ],
}

/** The entry to hover, so the highlight row lands on what this harness guards. */
const HOVER = { en: 'Show in file manager', 'zh-CN': '在文件管理器中显示' }

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Release notes review',
  running: false,
  messages: 4,
  agent: 'kirocrew',
  modified: Math.floor(Date.now() / 1000),
  last_ts: '2026-08-08T05:00:00Z',
  folder_id: '',
}]

/** File-read + library routes the panel and its ⋯ menu fetch on open.
 *
 * Must return TRUTHY to claim the route: the stub awaits this hook's result and
 * falls through to its own handler otherwise, and `route.fulfill()` resolves to
 * `undefined` — so returning it directly double-fulfils and throws.
 */
const extra = async (path, route) => {
  if (path.startsWith('/api/file-read')) {
    await route.fulfill({ status: 200, contentType: 'text/plain', body: DOC })
    return true
  }
  if (path.startsWith('/api/knowledge/sources')) { await json(route, { sources: [] }); return true }
  if (path.startsWith('/api/knowledge/config')) {
    await json(route, { enabled: true, supported_formats: ['.md', '.txt', '.pdf'] })
    return true
  }
  if (path.startsWith('/api/artifacts')) { await json(route, { artifacts: [] }); return true }
  return false
}

async function shoot(page, name, menu, width, height) {
  const box = await menu.boundingBox()
  const x = Math.max(0, box.x - 48)
  const y = Math.max(0, box.y - 56)
  await page.screenshot({
    path: `${OUT}/${name}.png`,
    clip: { x, y, width: width - x, height: Math.min(height - y, box.height + 80) },
  })
  console.log('wrote', `${OUT}/${name}.png`)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const width = 1400
  const height = 900
  const failures = []

  for (const [theme, lang] of [['dark', 'en'], ['light', 'en'], ['dark', 'zh-CN']]) {
    const context = await browser.newContext({
      viewport: { width, height },
      deviceScaleFactor: 2, // 13px menu type renders soft at 1x
    })
    const page = await context.newPage()
    logPageProblems(page)
    // The stub's init script clears localStorage, so every seed below has to be
    // registered AFTER it.
    await stubDashboardApi(page, { slots, theme, extra })
    await page.addInitScript(([slot, docPath, doc, lng]) => {
      localStorage.setItem('mc-active-slot', slot)
      localStorage.setItem('mc-activity-open:' + slot, 'true')
      localStorage.setItem('mc-privacy-notice-v1', '1')
      localStorage.setItem('mc-lang', lng)
      // `loadPersisted` reads tabs verbatim, so a seeded `content` renders
      // without a fetch (the strip happens on WRITE, in serializeBucket).
      localStorage.setItem('mc-panel-tabs:' + slot, JSON.stringify({
        activeId: 'doc',
        tabs: [{ id: 'doc', kind: 'file', title: 'release-notes.md', path: docPath, content: doc }],
      }))
    }, [SLOT, DOC_PATH, DOC, lang])

    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)

    // Prove the file panel itself rendered before blaming the menu for a miss.
    await page.getByText('release-notes.md').first().waitFor({ state: 'visible', timeout: 15000 })

    // `pages.chatSidebar.more_options` renders the SAME label on the sidebar
    // header and on every session row, so the accessible name is ambiguous —
    // three buttons match it on a chat page. The panel's trigger carries a
    // testid for exactly this reason.
    const trigger = page.locator('[data-testid="markdown-panel-more-options"]')
    await trigger.first().waitFor({ state: 'visible', timeout: 15000 })
    const n = await trigger.count()
    if (n !== 1) throw new Error(`expected exactly 1 panel ⋯ trigger, found ${n}`)
    await trigger.click()
    const menu = page.locator('[role="menu"]').last()
    await menu.waitFor({ state: 'visible', timeout: 10000 })
    await page.waitForTimeout(400)

    const items = (await menu.locator('[role="menuitem"]').allInnerTexts()).map(s => s.trim())
    console.log(`ITEMS ${theme}/${lang}`, JSON.stringify(items))
    const want = EXPECTED[lang]
    if (JSON.stringify(items) !== JSON.stringify(want)) {
      failures.push(`${theme}/${lang}: inventory drifted\n      want ${JSON.stringify(want)}\n      got  ${JSON.stringify(items)}`)
    }

    // Hover the restored pair so the highlight row lands on the change itself.
    await menu.getByText(HOVER[lang], { exact: true }).hover()
    await page.waitForTimeout(150)

    // The non-English pass is a CATALOG assertion, not a picture: this capture
    // host ships only DejaVu, so CJK glyphs render as tofu boxes and a zh-CN
    // screenshot would look like a bug it does not have. The asserted item list
    // above is the real evidence that the three new keys resolve.
    if (lang === 'en') {
      await page.screenshot({ path: `${OUT}/00-window-${theme}.png` })
      await shoot(page, `01-menu-${theme}`, menu, width, height)
    } else {
      console.log(`(no image for ${lang}: capture host has no CJK font — labels asserted above)`)
    }
    await context.close()
  }

  await browser.close()
  srv.close()

  if (failures.length) {
    console.error('FAIL\n  ' + failures.join('\n  '))
    process.exit(1)
  }
  console.log('OK: full menu inventory matches in every theme/locale captured')
}

main().catch(err => { console.error(err); process.exit(1) })
