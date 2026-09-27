/**
 * Screenshot harness for the two LIQUID GLASS surfaces: the chat composer and
 * the mobile Settings bottom search capsule. Photographs the REAL built SPA
 * (website/dist) over a stubbed dashboard API in both polarities, so the
 * frosted --glass-tint, the top/bottom specular band and the composer halo are
 * the shipped ones, not a mock. Nothing in CI runs this file.
 *
 * Usage: node scripts/capture-liquid-glass.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/liquid-glass'
const SLOT = 'chat-glass'
const PROJECT = '/home/user/workspace/notes'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Liquid glass for the composer',
  running: false,
  last_message: 'Here is what the material does.',
  messages: 4,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 4,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 900, content: '网络上有没有好用的 liquid glass library？' },
    {
      role: 'assistant', ts: Date.now() / 1000 - 800,
      content: '有。网页端最常用的是 **liquid-glass-react**（6.3k ★，已停更）和 **samasante/liquid-glass**（跨浏览器，在维护）。\n\n```bash\nnpx liquid-glass-cli add liquid-glass\n```\n\n苹果 HIG 只允许 Liquid Glass 用在控件层和导航层，不进内容层。',
    },
    { role: 'user', ts: Date.now() / 1000 - 120, content: '用在移动端设置页的底部搜索胶囊，和聊天输入框。' },
    {
      role: 'assistant', ts: Date.now() / 1000 - 30,
      content: '好。两处都是控件层，符合规范。底色跟主题走：浅色白 72%，深色黑 55%；高光沿上下边均匀，不带中心亮点。',
    },
  ],
}


/** The glass is live when an effect layer under `root` carries a backdrop blur. */
async function assertGlass(page, root, label) {
  const filters = await root.evaluate(el => Array.from(el.querySelectorAll('*'))
    .map(n => getComputedStyle(n).backdropFilter).filter(f => f && f !== 'none'))
  if (!filters.some(f => /blur\(/.test(f))) throw new Error(`${label}: no backdrop blur layer rendered (${filters.join(' | ') || 'none'})`)
  console.log(label, 'backdrop layers:', filters.join(' | '))
}


/** A dense transcript, so text and code sit under the composer while scrolling. */
const LONG_PARA = '玻璃永远浮在内容层之上。内容滚过去的时候，模糊、着色和上下两条高光要一起把文字压住，但又不能把它盖死。这一段的目的就是给底下垫足够多的字，看看 --glass-tint 在真实文字上是什么密度。The quick brown fox jumps over the lazy dog while the frosted pane keeps every glyph half-visible beneath it.'
const longDetail = {
  ...detail,
  total: 40,
  messages: Array.from({ length: 40 }, (_, i) => i % 2 === 0
    ? { role: 'user', ts: Date.now() / 1000 - (40 - i) * 60, content: `第 ${i / 2 + 1} 个问题：${LONG_PARA.slice(0, 60)}` }
    : { role: 'assistant', ts: Date.now() / 1000 - (40 - i) * 60, content: `${LONG_PARA}\n\n${LONG_PARA}\n\n\`\`\`ts\nexport function specularRing(peak: number, radius: number) {\n  const r1 = radius * 0.7\n  const r2 = radius * 1.5\n  return \`linear-gradient(to bottom, ...)\`\n}\n\`\`\`\n\n- 底色 tint 跟主题走\n- 高光沿上下边均匀\n- 折射 50，只在边缘` }),
}


/** A tool approval is pending, so the approval bar is fused to the composer's top
 *  and the wrapper returns to its solid surface under it. No tool_call_id: with no
 *  in-transcript pill to defer to, the bar never yields to its ghost, so the
 *  fused state is what renders. */
const approvalDetail = {
  ...detail,
  running: true,
  messages: [
    ...detail.messages,
    {
      role: 'permission', ts: Date.now() / 1000 - 5, content: 'Running: ls /tmp',
      meta: { approval_id: 'ap-glass-1', request_id: 'req-1', tool_input: '{"command":"ls /tmp"}', is_read_only: '1', tool_title: 'Running: ls /tmp', is_shell: '1', full_command: 'ls /tmp', base_command: 'ls', trust_command_grantable: '1', trust_base_grantable: '1', trust_grantable: '1' },
    },
  ],
}
/** Incognito memory mode: the warn border stays, the surface is now the glass. */
const incognitoSlots = slots.map(s => ({ ...s, memory_mode: 'incognito' }))

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  let activeDetail = detail
  const extra = async (path, route) => {
    if (path.startsWith('/api/chat/slots/')) { await json(route, activeDetail); return true }
    return false
  }

  async function chat(theme, variant = '') {
    activeDetail = variant === 'long' ? longDetail : variant === 'approval' ? approvalDetail : detail
    const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { slots: variant === 'incognito' ? incognitoSlots : slots, theme, extra })
    await page.addInitScript(slot => { localStorage.setItem('mc-active-slot', slot) }, SLOT)
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
    if (variant === 'long') {
      // Scroll the transcript so a message body, not the tail padding, sits under the glass.
      await page.evaluate(() => { const el = document.querySelector('[data-testid="message-list"], main [class*="overflow-y-auto"]'); if (el) el.scrollTop = el.scrollHeight - el.clientHeight - 180 })
      await page.waitForTimeout(600)
    }
    const dialogs = await page.getByRole('dialog').count()
    if (dialogs) throw new Error(`chat/${theme}: ${dialogs} unexpected dialog(s) open`)
    if (variant === 'approval' && !(await page.getByRole('button', { name: /allow once/i }).count())) throw new Error(`chat/${theme}/approval: approval bar missing`)
    if (variant === 'incognito') {
      const cls = await page.getByTestId('input-wrapper').first().getAttribute('class')
      if (!/border-warn/.test(cls ?? '')) throw new Error(`chat/${theme}/incognito: warn border missing`)
    }
    const box = await page.getByTestId('input-wrapper').first().boundingBox()
    if (!box) throw new Error(`chat/${theme}: input-wrapper missing`)
    await assertGlass(page, page.getByTestId('input-wrapper').first().locator('xpath=../..'), `chat/${theme}`)
    if (variant === 'long') {
      const under = await page.evaluate(b => Array.from(document.querySelectorAll('.msg-content p, .msg-content code, .msg-content li'))
        .filter(n => { const r = n.getBoundingClientRect(); return r.height > 0 && r.bottom > b.y && r.top < b.y + b.height && r.right > b.x && r.left < b.x + b.width }).length, box)
      console.log(`chat/${theme}/long: ${under} text node(s) sit under the glass`)
    }
    if (!variant) {
      // Rest state first: the composer autofocuses, and a focused frame hides
      // whether the pane has an outline of its own.
      await page.mouse.click(700, 200)
      await page.waitForTimeout(300)
      await page.screenshot({
        path: `${OUT}/composer-${theme}-rest-crop.png`,
        clip: { x: Math.max(0, box.x - 40), y: Math.max(0, box.y - 140), width: box.width + 80, height: box.height + 180 },
      })
      await page.getByLabel('Message input').first().click()
      await page.waitForTimeout(300)
    }
    await page.screenshot({ path: `${OUT}/composer-${theme}${variant ? '-' + variant : ''}.png` })
    await page.screenshot({
      path: `${OUT}/composer-${theme}${variant ? '-' + variant : ''}-crop.png`,
      clip: { x: Math.max(0, box.x - 40), y: Math.max(0, box.y - 200), width: box.width + 80, height: box.height + 240 },
    })
    console.log('wrote', `${OUT}/composer-${theme}${variant ? '-' + variant : ''}.png`)
    await context.close()
  }

  async function settingsMobile(theme) {
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true,
    })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { slots, theme, extra })
    await page.goto(base + '/settings', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
    const dialogs = await page.getByRole('dialog').count()
    if (dialogs) throw new Error(`settings/${theme}: ${dialogs} unexpected dialog(s) open`)
    const search = page.getByPlaceholder(/search/i).first()
    const box = await search.boundingBox()
    if (!box) throw new Error(`settings/${theme}: search capsule missing`)
    await assertGlass(page, search.locator('xpath=ancestor::*[contains(@class,"composer-halo")][1]'), `settings/${theme}`)
    const halo = search.locator('xpath=ancestor::*[contains(@class,"composer-halo")][1]')
    const restShadow = await halo.evaluate(el => getComputedStyle(el).boxShadow)
    await page.screenshot({ path: `${OUT}/settings-capsule-${theme}.png` })
    console.log('wrote', `${OUT}/settings-capsule-${theme}.png`)
    // Focused: the capsule's focus cue is the halo box's accent glow (the input
    // itself has no outline), so the glow must change when the input takes focus.
    await search.tap()
    await page.waitForTimeout(400)
    const focused = await search.evaluate(el => document.activeElement === el)
    if (!focused) throw new Error(`settings/${theme}: search input did not take focus`)
    const focusShadow = await halo.evaluate(el => getComputedStyle(el).boxShadow)
    if (focusShadow === restShadow) throw new Error(`settings/${theme}: halo glow unchanged on focus (${focusShadow})`)
    console.log(`settings/${theme}: halo ${restShadow} -> ${focusShadow}`)
    await page.screenshot({ path: `${OUT}/settings-capsule-${theme}-focused.png` })
    console.log('wrote', `${OUT}/settings-capsule-${theme}-focused.png`)
    await context.close()
  }


  async function settingsDesktop(theme) {
    const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { slots, theme, extra })
    await page.goto(base + '/settings?tab=display', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
    const dialogs = await page.getByRole('dialog').count()
    if (dialogs) throw new Error(`settings-desktop/${theme}: ${dialogs} unexpected dialog(s) open`)
    await page.screenshot({ path: `${OUT}/settings-desktop-${theme}.png` })
    console.log('wrote', `${OUT}/settings-desktop-${theme}.png`)
    await context.close()
  }

  for (const theme of ['dark', 'light']) {
    await chat(theme)
    await chat(theme, 'long')
    await chat(theme, 'approval')
    await chat(theme, 'incognito')
    await settingsMobile(theme)
    await settingsDesktop(theme)
  }

  await browser.close()
  srv.close()
  console.log('OK')
}

main().catch(err => { console.error(err); process.exit(1) })
