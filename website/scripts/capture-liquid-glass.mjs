/**
 * Screenshot harness for the LIQUID GLASS surfaces: the chat composer dock
 * (composer, follow-up chips at rest and picked, tool and sub-agent approvals,
 * tip / folder-suggestion / question cards, memory chip, jump-to-bottom pill,
 * the transcript scrolling under it) and the mobile Settings bottom search
 * capsule. Photographs the REAL built SPA (website/dist) over a stubbed
 * dashboard API in both polarities, so the frosted --glass-tint, the top/bottom
 * specular band and the neutral glass-shadow are the shipped ones, not a mock. The
 * long-transcript scene also asserts the floating-dock geometry, and the
 * `spawn-flow` scene records the approval band's three-step transition as a
 * GIF (needs ffmpeg on PATH). Nothing in CI runs this file.
 *
 * Usage: node scripts/capture-liquid-glass.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync, unlinkSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { screenshotWithCaret } from './lib/screenshot-with-caret.mjs'


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

/** Last turn ends with an [OPTIONS:] line, so the follow-up chips render above the composer. */
const OPTIONS = ['用 A', '用 B', '再调一下高光']
const chipsDetail = {
  ...detail,
  messages: [
    ...detail.messages.slice(0, -1),
    { role: 'assistant', ts: Date.now() / 1000 - 30, content: `三个方案都渲染好了。\n\n[OPTIONS: ${OPTIONS.join(' | ')}]` },
  ],
}
/** Incognito memory mode: the warn border stays, the surface is now the glass. */
const incognitoSlots = slots.map(s => ({ ...s, memory_mode: 'incognito' }))
/** Incognito on an EMPTY session, so the memory chip renders in its warn form. */
const incognitoWelcomeSlots = incognitoSlots.map(s => ({ ...s, messages: 0, last_message: '' }))

/** An empty session: the welcome hero plus the memory-mode chip in its default state. */
const welcomeDetail = { ...detail, total: 0, messages: [] }
const welcomeSlots = slots.map(s => ({ ...s, messages: 0, last_message: '' }))

/** A live turn with no approval, so the tip card is what the 10s gate lets through. */
const runningDetail = { ...longDetail, running: true }
const TIP = { id: 'tip-glass', feature: 'memory', title: 'Pin what matters', body: '记忆模式决定这段对话有多少会被留下来。切到 Incognito 就什么都不存。', why: '', doc: '', cta_prompt: '', action: null }

/** Frames pushed over the (otherwise swallowed) websocket to raise the cards the
 *  REST stubs cannot: the question card, the folder suggestion, a spawn approval.
 *  A scene may list several frames; they are sent in order, 900ms apart. */
const spawnFrame = { type: 'approval', data: { id: 'spawn:ag-glass', slot: SLOT, tool: 'spawn_run(review the dock layout from a new-user perspective)', tool_input: '', source: 'agent', ts: Date.now() / 1000 } }
/** The user sends a message while the spawn is still pending. The approval bar
 *  scans only the rows after the last user message, so this is the path on which
 *  the spawn card carries the decision itself: a spawn approval frame ALSO mints
 *  the bar's permission row (useWebSocket `approval` -> `sseChatMessage`), so with
 *  no user message after it the card always defers to the bar. */
const userFrame = { type: 'chat_message', data: { slot: SLOT, role: 'user', content: '先别等我，你接着往下做。', ts: Date.now() / 1000 } }
/** The bar's own tool approval (approvalDetail's `ap-glass-1`) is decided. */
const toolResolvedFrame = { type: 'approval_resolved', data: { id: 'ap-glass-1', slot: SLOT, approved: true } }
const wsFrames = {
  question: { type: 'question_card', data: { slot: SLOT, card_id: 'q-glass', questions: [{ header: 'GLASS', question: '边缘折射用哪一档？', options: [{ label: '25', description: '几乎看不出' }, { label: '50', description: '细线会弯' }, { label: '80' }] }] } },
  folder: { type: 'slot_folder_suggestion', data: { slot: SLOT, folder_id: 'f-kiro', folder_name: 'Kiro', breadcrumb: 'Work / Kiro' } },
  // Live card: the spawn's request is on the bar, then the user speaks and the
  // bar hides; the card is now the only surface and shows Approve / Reject.
  spawn: [spawnFrame, userFrame],
  // Withheld card: a tool approval AND the spawn are pending; the bar acts, the
  // card names the count and links to the panel.
  'spawn-both': spawnFrame,
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  let activeDetail = detail
  const extra = async (path, route) => {
    if (path.startsWith('/api/chat/slots/')) { await json(route, activeDetail); return true }
    return false
  }

  async function chat(theme, variant = '') {
    activeDetail = variant === 'long' || variant === 'reduce-long' || variant === 'question' || variant === 'folder' ? longDetail
      : variant === 'approval' || variant === 'spawn-both' ? approvalDetail
      : variant === 'spawn' || variant === 'tip' ? runningDetail
      : variant === 'chips' || variant === 'chips-picked' || variant === 'reduce' ? chipsDetail
      : variant === 'welcome' || variant === 'incognito' ? welcomeDetail
      : detail
    const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    const tipsExtra = async (path, route) => {
      if (path === '/api/tips/status') { await json(route, { enabled_config: true, opted_out: false, cadence_hours: 6 }); return true }
      if (path === '/api/tips/next') { await json(route, { tip: TIP, glow: false }); return true }
      return extra(path, route)
    }
    await stubDashboardApi(page, { slots: variant === 'incognito' ? incognitoWelcomeSlots : variant === 'welcome' ? welcomeSlots : slots, theme, extra: variant === 'tip' ? tipsExtra : extra })
    // Registered AFTER the stub's swallow route so it wins: the socket opens
    // against nothing and we push the scene's frame(s) into it once the page is up.
    const frames = [].concat(wsFrames[variant] ?? [])
    if (frames.length) await page.routeWebSocket(/\/api\/ws/, ws => { frames.forEach((f, i) => setTimeout(() => ws.send(JSON.stringify(f)), 1500 + i * 900)) })
    await page.addInitScript(slot => { localStorage.setItem('mc-active-slot', slot) }, SLOT)
    // The collapsed composer is a persisted per-browser choice (ChatInput's
    // COMPOSER_COLLAPSED_LS_KEY); seed it so the dock comes up as the bar.
    if (variant === 'collapsed') await page.addInitScript(() => { localStorage.setItem('mc-composer-collapsed', '1') })
    // The user's own switch (Settings -> Display -> Reduce glass transparency):
    // the index.html bootstrap reads this key and sets data-reduce-transparency
    // before hydration, so the first paint is already solid.
    if (variant === 'reduce' || variant === 'reduce-long') await page.addInitScript(() => { localStorage.setItem('mc-reduce-transparency', 'on') })
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    // The tip gate is 10s; a multi-frame scene needs its last frame landed and painted.
    await page.waitForTimeout(variant === 'tip' ? 12500 : 2500 + Math.max(0, frames.length - 1) * 900 + 500)
    if (variant === 'long') {
      // Floating-dock layout: the scroller runs to the pane's bottom edge and pays
      // for the covered strip with padding = dock height + clearance, so at the
      // bottom the last line stops clear of the glass.
      const geo = await page.evaluate(() => {
        const dock = document.querySelector('[data-testid="composer-dock-root"]')
        const sc = document.querySelector('.chat-container')
        if (!dock || !sc) return null
        const d = dock.getBoundingClientRect(), s = sc.getBoundingClientRect()
        const bodies = Array.from(document.querySelectorAll('.msg-content')).map(n => n.getBoundingClientRect().bottom).filter(b => b > 0)
        return { dockTop: d.top, dockBottom: d.bottom, dockH: d.height, scrollerBottom: s.bottom, pad: parseFloat(getComputedStyle(sc).paddingBottom), lastBodyBottom: Math.max(...bodies), atBottom: Math.abs(sc.scrollHeight - sc.clientHeight - sc.scrollTop) < 2 }
      })
      if (!geo) throw new Error(`chat/${theme}/long: dock root or scroller missing`)
      if (Math.abs(geo.scrollerBottom - geo.dockBottom) > 1) throw new Error(`chat/${theme}/long: scroller stops at ${geo.scrollerBottom}, dock at ${geo.dockBottom} -- transcript does not run under the dock`)
      if (Math.abs(geo.pad - (Math.round(geo.dockH) + 16)) > 1) throw new Error(`chat/${theme}/long: scroller padding ${geo.pad} != dock ${geo.dockH} + 16`)
      if (!geo.atBottom) throw new Error(`chat/${theme}/long: not pinned to the bottom on entry`)
      const clearance = geo.dockTop - geo.lastBodyBottom
      if (clearance < 16) throw new Error(`chat/${theme}/long: last line clears the dock by ${clearance}px (< 16)`)
      console.log(`chat/${theme}/long: scroller runs under the dock (dock ${Math.round(geo.dockH)}px, pad ${geo.pad}px); last line clears it by ${Math.round(clearance)}px`)
      await page.screenshot({ path: `${OUT}/composer-${theme}-long-bottom.png` })
      console.log('wrote', `${OUT}/composer-${theme}-long-bottom.png`)
      // Input falls through the dock beside the content column: the dock root
      // stops short of the scroller's reserved scrollbar gutter, and a wheel
      // delivered to the empty width left of the column (the wrapper boxes are
      // `dock-inert`) scrolls the transcript underneath.
      const gutter = await page.evaluate(() => {
        const dock = document.querySelector('[data-testid="composer-dock-root"]')
        const sc = document.querySelector('.chat-container')
        const d = dock.getBoundingClientRect(), s = sc.getBoundingClientRect()
        const col = document.querySelector('.input-area').getBoundingClientRect()
        return { dockRight: d.right, scrollerRight: s.right, reserved: sc.offsetWidth - sc.clientWidth, dockLeft: d.left, colLeft: col.left, probeY: d.bottom - 30, before: sc.scrollTop }
      })
      if (Math.abs((gutter.scrollerRight - gutter.dockRight) - gutter.reserved) > 1) throw new Error(`chat/${theme}/long: dock right edge ${gutter.dockRight} vs scroller ${gutter.scrollerRight}; reserved gutter ${gutter.reserved}px -- the dock covers the scrollbar column`)
      if (gutter.colLeft - gutter.dockLeft > 24) {
        const x = gutter.dockLeft + (gutter.colLeft - gutter.dockLeft) / 2
        const hit = await page.evaluate(([x, y]) => document.elementFromPoint(x, y)?.className ?? '', [x, gutter.probeY])
        if (/dock-inert|composer-status-stack|input-area/.test(hit)) throw new Error(`chat/${theme}/long: the dock gutter hit-tests to "${hit}", not the transcript`)
        await page.mouse.move(x, gutter.probeY)
        await page.mouse.wheel(0, -120)
        await page.waitForTimeout(250)
        const after = await page.evaluate(() => document.querySelector('.chat-container').scrollTop)
        if (!(after < gutter.before - 20)) throw new Error(`chat/${theme}/long: wheel over the dock gutter did not scroll the transcript (${gutter.before} -> ${after})`)
        await page.evaluate(() => { const el = document.querySelector('.chat-container'); el.scrollTop = el.scrollHeight })
        await page.waitForTimeout(250)
        console.log(`chat/${theme}/long: dock stops ${gutter.reserved}px short of the scrollbar column; wheel in the ${Math.round(gutter.colLeft - gutter.dockLeft)}px gutter scrolled the transcript ${Math.round(gutter.before - after)}px`)
      }
      // Scroll up far enough that a message body runs under the WHOLE dock,
      // the context shelf included: the shelf sits below the glass on the
      // bare transcript and stands on the `glass-shelf` fade (nothing at the
      // pane's bottom edge, page colour by 60% of its height), so a chip label
      // never reads against text scrolling under it. Assert the fade is there,
      // starts at the pane's edge and does not touch the pane, then photograph
      // the dock's bottom edge.
      const shelfGeo = await page.evaluate(() => {
        const el = document.querySelector('.chat-container'); el.scrollTop = el.scrollHeight - el.clientHeight - 420
        const shelf = document.querySelector('[data-testid="composer-context-shelf"]')
        const dock = document.querySelector('[data-testid="composer-dock"]')
        if (!shelf || !dock) return null
        const before = getComputedStyle(shelf, '::before')
        const s = shelf.getBoundingClientRect(), d = dock.getBoundingClientRect()
        return { bg: before.backgroundImage, z: before.zIndex, top: s.top, dockBottom: d.bottom, dockLeft: d.left, dockWidth: d.width, shelfBottom: s.bottom, dockBg: getComputedStyle(dock, '::before').backgroundImage }
      })
      if (!shelfGeo) throw new Error(`chat/${theme}/long: shelf or dock missing`)
      if (!/linear-gradient\(/.test(shelfGeo.bg) || shelfGeo.z !== '-1') throw new Error(`chat/${theme}/long: shelf has no fade behind it (${shelfGeo.bg} z=${shelfGeo.z})`)
      if (shelfGeo.top - shelfGeo.dockBottom > 1) throw new Error(`chat/${theme}/long: the fade starts ${shelfGeo.top - shelfGeo.dockBottom}px below the pane, not at its edge`)
      if (/linear-gradient\(/.test(shelfGeo.dockBg)) throw new Error(`chat/${theme}/long: the pane itself carries a fade (${shelfGeo.dockBg})`)
      await page.waitForTimeout(600)
      await page.screenshot({
        path: `${OUT}/composer-${theme}-long-shelf-crop.png`,
        clip: { x: Math.max(0, shelfGeo.dockLeft - 40), y: Math.max(0, shelfGeo.dockBottom - 120), width: shelfGeo.dockWidth + 80, height: (shelfGeo.shelfBottom - shelfGeo.dockBottom) + 150 },
      })
      console.log('wrote', `${OUT}/composer-${theme}-long-shelf-crop.png`)
      // The same position, whole page: text under every part of the dock.
      await page.screenshot({ path: `${OUT}/composer-${theme}-long-under.png` })
      console.log('wrote', `${OUT}/composer-${theme}-long-under.png`)
      // Now scroll up so a message body, not the tail padding, sits under the glass.
      await page.evaluate(() => { const el = document.querySelector('.chat-container'); if (el) el.scrollTop = el.scrollHeight - el.clientHeight - 180 })
      await page.waitForTimeout(600)
    }
    if (variant === 'reduce-long') {
      // The switch's before/after partner to `long`: the same transcript at the
      // same scroll position, so the pair shows the one difference -- text
      // refracting through the glass vs. a solid card covering it.
      await page.evaluate(() => { const el = document.querySelector('.chat-container'); if (el) el.scrollTop = el.scrollHeight - el.clientHeight - 180 })
      await page.waitForTimeout(600)
    }
    const dialogs = await page.getByRole('dialog').count()
    if (dialogs) throw new Error(`chat/${theme}: ${dialogs} unexpected dialog(s) open`)
    if (variant === 'approval' && !(await page.getByRole('button', { name: /allow once/i }).count())) throw new Error(`chat/${theme}/approval: approval bar missing`)
    if (variant === 'spawn-both') {
      // Tool approval AND a spawn approval pending together: one set of decision
      // buttons on screen. The spawn card keeps its count and the panel link,
      // withholds Approve/Reject and its glow; the dock carries the glow.
      if (!(await page.getByRole('button', { name: /allow once/i }).count())) throw new Error(`chat/${theme}/spawn-both: approval bar missing`)
      if (!(await page.getByText(/^1 sub-agent pending — answer the request below first$/).count())) throw new Error(`chat/${theme}/spawn-both: spawn card missing or still says "awaiting your approval" beside the tool bar`)
      if (await page.getByTestId('spawn-approval-card').getByRole('button', { name: /^Approve$/ }).count()) throw new Error(`chat/${theme}/spawn-both: spawn card still offers Approve beside the tool bar`)
      if (!(await page.getByTestId('spawn-approval-card').getByRole('button', { name: /review in panel/i }).count())) throw new Error(`chat/${theme}/spawn-both: spawn card lost its panel link`)
      if (/approval-glow/.test(await page.getByTestId('spawn-approval-card').getAttribute('class'))) throw new Error(`chat/${theme}/spawn-both: spawn card still glows beside the tool bar`)
    }
    if (variant === 'spawn') {
      // Live card, alone: the bar is gone (the user spoke after the request), so
      // the card carries the decision and the glow.
      if (await page.getByRole('button', { name: /allow once/i }).count()) throw new Error(`chat/${theme}/spawn: approval bar still up -- the user frame did not hide it`)
      if (!(await page.getByText(/1 sub-agent is awaiting your approval to run/).count())) throw new Error(`chat/${theme}/spawn: live card copy missing`)
      if (!(await page.getByTestId('spawn-approval-card').getByRole('button', { name: /^Approve$/ }).count())) throw new Error(`chat/${theme}/spawn: live card offers no Approve`)
      if (!(await page.getByTestId('spawn-approval-card').getByRole('button', { name: /^Reject$/ }).count())) throw new Error(`chat/${theme}/spawn: live card offers no Reject`)
      if (!/approval-glow/.test(await page.getByTestId('spawn-approval-card').getAttribute('class'))) throw new Error(`chat/${theme}/spawn: live card has no glow`)
    }
    if (variant === 'chips') {
      // Every chip IS a Liquid Glass pane (the same primitive as the composer):
      // a plain chip renders the pane AS the button, a split chip (instant-send
      // on) renders it as the wrapper the two buttons sit on. Either way the
      // chip sits inside exactly one pane and its frost layer is live.
      for (const o of OPTIONS) {
        const chip = page.getByRole('button', { name: o, exact: true })
        if (!(await chip.count())) throw new Error(`chat/${theme}/chips: chip "${o}" missing`)
        const pane = chip.locator('xpath=ancestor-or-self::*[contains(concat(" ", normalize-space(@class), " "), " liquid-glass ")][1]')
        if (!(await pane.count())) throw new Error(`chat/${theme}/chips: chip "${o}" is not on a Liquid Glass pane`)
        await assertGlass(page, pane, `chat/${theme}/chips "${o}"`)
      }
      console.log(`chat/${theme}/chips: ${await page.locator('[data-testid="composer-dock-root"] .liquid-glass').count()} glass pane(s) rendered`)
    }
    if (variant === 'incognito') {
      const cls = await page.getByTestId('input-wrapper').first().getAttribute('class')
      if (!/border-warn/.test(cls ?? '')) throw new Error(`chat/${theme}/incognito: warn border missing`)
      // The memory chip in its warn form: same glass, the warn hue mixed into the tint.
      if (!(await page.locator('[data-testid="memory-mode-chip"].liquid-glass.glass-warn').count())) throw new Error(`chat/${theme}/incognito: warn memory chip missing`)
    }
    if (variant === 'chips-picked') {
      await page.getByRole('button', { name: OPTIONS[1], exact: true }).click()
      await page.waitForTimeout(400)
      if (!(await page.locator('.liquid-glass.glass-accent').count())) throw new Error(`chat/${theme}/chips-picked: no accent (picked) chip rendered`)
    }
    const mustShow = {
      welcome: ['[data-testid="composer-memory-chip"] .liquid-glass', 'memory chip'],
      tip: ['[data-testid="tip-card"]', 'tip card'],
      question: ['[data-testid="composer-dock-root"] .liquid-glass.glass-accent', 'question card'],
      folder: ['[data-testid="folder-suggestion-card"]', 'folder suggestion card'],
      spawn: ['[data-testid="spawn-approval-card"].approval-glow', 'sub-agent approval card'],
      collapsed: ['[data-testid="composer-collapsed-bar"]', 'collapsed composer bar'],
    }[variant]
    if (mustShow && !(await page.locator(mustShow[0]).count())) throw new Error(`chat/${theme}/${variant}: ${mustShow[1]} missing`)
    if (variant === 'question' && !(await page.getByRole('button', { name: '50' }).count())) throw new Error(`chat/${theme}/question: question options missing`)
    if (variant === 'collapsed') {
      // The bar is the whole dock: no input-wrapper is mounted, and the bar
      // itself is transparent, so the edge the reader sees is the dock's.
      if (await page.getByTestId('input-wrapper').count()) throw new Error(`chat/${theme}/collapsed: editor still mounted behind the bar`)
      if (!(await page.getByRole('button', { name: /show the message input/i }).count())) throw new Error(`chat/${theme}/collapsed: "Show the message input" not exposed as a button`)
    }
    const box = await (variant === 'collapsed' ? page.getByTestId('composer-dock') : page.getByTestId('input-wrapper')).first().boundingBox()
    if (!box) throw new Error(`chat/${theme}: ${variant === 'collapsed' ? 'composer-dock' : 'input-wrapper'} missing`)
    if (variant === 'reduce' || variant === 'reduce-long') {
      // Same rules as the OS fallback: every pane a solid --bg-elevated card,
      // effect layers hidden, no backdrop blur anywhere in the dock.
      const st = await page.evaluate(() => {
        const html = document.documentElement.dataset.reduceTransparency
        const dock = document.querySelector('[data-testid="composer-dock"]')
        const layers = Array.from(dock.querySelectorAll(':scope > [data-liquid-glass-layer]')).map(l => getComputedStyle(l).display)
        const chips = Array.from(document.querySelectorAll('[data-testid="composer-dock-root"] .liquid-glass')).map(el => getComputedStyle(el).backgroundColor)
        return { html, dockBg: getComputedStyle(dock).backgroundColor, layers, chips }
      })
      if (st.html !== 'on') throw new Error(`chat/${theme}/reduce: data-reduce-transparency not applied (${st.html})`)
      if (st.layers.some(d => d !== 'none')) throw new Error(`chat/${theme}/reduce: a glass layer still paints (${st.layers.join(',')})`)
      if (st.dockBg === 'rgba(0, 0, 0, 0)') throw new Error(`chat/${theme}/reduce: dock has no solid fill`)
      if (st.chips.some(c => c === 'rgba(0, 0, 0, 0)')) throw new Error(`chat/${theme}/reduce: a pane is still transparent (${st.chips.join(' | ')})`)
      console.log(`chat/${theme}/${variant}: ${st.chips.length} panes solid (${st.dockBg}), ${st.layers.length} layers hidden`)
    } else {
      await assertGlass(page, page.getByTestId('composer-dock').first(), `chat/${theme}`)
    }
    if (variant === 'long' || variant === 'reduce-long') {
      const under = await page.evaluate(b => Array.from(document.querySelectorAll('.msg-content p, .msg-content code, .msg-content li'))
        .filter(n => { const r = n.getBoundingClientRect(); return r.height > 0 && r.bottom > b.y && r.top < b.y + b.height && r.right > b.x && r.left < b.x + b.width }).length, box)
      if (under < 1) throw new Error(`chat/${theme}/${variant}: nothing sits under the dock -- the transcript is not scrolling beneath it`)
      if (!(await page.getByRole('button', { name: /scroll to bottom/i }).count())) throw new Error(`chat/${theme}/${variant}: jump-to-bottom pill missing while scrolled up`)
      console.log(`chat/${theme}/${variant}: ${under} text node(s) sit under the dock`)
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
      const dockEl = page.getByTestId('composer-dock').first()
      const wrapEl = page.getByTestId('input-wrapper').first()
      const restDock = await dockEl.evaluate(el => ({ shadow: getComputedStyle(el).boxShadow, tint: getComputedStyle(el).getPropertyValue('--glass-tint').trim(), edge: getComputedStyle(el).getPropertyValue('--glass-edge').trim() }))
      const restBorder = await wrapEl.evaluate(el => getComputedStyle(el).borderTopColor)
      await page.getByLabel('Message input').first().click()
      await page.waitForTimeout(300)
      // Focus changes NOTHING on the pane (maintainer decision): same shadow,
      // same tint, same side lines, and the wrapper border stays transparent --
      // no accent glow, no accent border, no neutral step either. The caret is
      // the composer's focus indicator.
      const focusDock = await dockEl.evaluate(el => ({ shadow: getComputedStyle(el).boxShadow, tint: getComputedStyle(el).getPropertyValue('--glass-tint').trim(), edge: getComputedStyle(el).getPropertyValue('--glass-edge').trim() }))
      const focusBorder = await wrapEl.evaluate(el => getComputedStyle(el).borderTopColor)
      if (!/^rgba\(0, 0, 0, [\d.]+\) 0px 0px 18px 0px$/.test(restDock.shadow)) throw new Error(`chat/${theme}: dock shadow is not the neutral glass-shadow: ${restDock.shadow}`)
      if (focusDock.shadow !== restDock.shadow) throw new Error(`chat/${theme}: dock shadow changed on focus (${restDock.shadow} -> ${focusDock.shadow}); the pane must not change on focus`)
      if (focusDock.tint !== restDock.tint) throw new Error(`chat/${theme}: dock tint changed on focus (${restDock.tint} -> ${focusDock.tint}); a focused pane is the same glass as a resting one`)
      if (focusDock.edge !== restDock.edge) throw new Error(`chat/${theme}: dock side line changed on focus (${restDock.edge} -> ${focusDock.edge}); the pane must not change on focus`)
      if (focusBorder !== restBorder) throw new Error(`chat/${theme}: wrapper border changed on focus (${restBorder} -> ${focusBorder}); the accent focus border is back`)
      console.log(`chat/${theme}: focus leaves the pane unchanged -- shadow ${focusDock.shadow}; edge ${focusDock.edge}; tint ${focusDock.tint}; border ${focusBorder}`)
    }
    if (variant === 'approval') {
      // A pending decision keeps the warm glow in the shadow slot at rest and
      // while the textarea has focus; the pane itself does not change on focus.
      await page.mouse.click(700, 200)
      await page.waitForTimeout(300)
      const dockEl = page.getByTestId('composer-dock').first()
      const read = () => dockEl.evaluate(el => ({ shadow: getComputedStyle(el).boxShadow, tint: getComputedStyle(el).getPropertyValue('--glass-tint').trim(), edge: getComputedStyle(el).getPropertyValue('--glass-edge').trim() }))
      const rest = await read()
      await page.getByLabel('Message input').first().click()
      await page.waitForTimeout(300)
      const focus = await read()
      if (/rgba\(0, 0, 0, [\d.]+\) 0px 0px 18px 0px/.test(rest.shadow)) throw new Error(`chat/${theme}/approval: the neutral glass-shadow displaced the approval glow at rest (${rest.shadow})`)
      if (/rgba\(0, 0, 0, [\d.]+\) 0px 0px 18px 0px/.test(focus.shadow)) throw new Error(`chat/${theme}/approval: the neutral glass-shadow displaced the approval glow on focus (${focus.shadow})`)
      if (focus.tint !== rest.tint) throw new Error(`chat/${theme}/approval: dock tint changed on focus (${rest.tint} -> ${focus.tint})`)
      if (focus.edge !== rest.edge) throw new Error(`chat/${theme}/approval: dock edge changed on focus (${rest.edge} -> ${focus.edge}); the pane must not change on focus`)
      // (The glow pulses, so its two reads differ by design; the two checks above
      // already pin that neither read is the neutral glass-shadow.)
      console.log(`chat/${theme}/approval: focus leaves the pane unchanged -- edge ${focus.edge}; tint ${focus.tint}; shadow stays the glow`)
      // The glow pulses inside this clip, so the caret probe is the textarea
      // alone. While a decision is pending the textarea may refuse focus (the
      // approval bar owns the keyboard); then there is no caret to catch and the
      // frame is taken plain.
      const approvalClip = { x: Math.max(0, box.x - 40), y: Math.max(0, box.y - 140), width: box.width + 80, height: box.height + 180 }
      const ta = page.getByLabel('Message input').first()
      if (await ta.evaluate(el => document.activeElement === el)) {
        // Best effort here: this frame documents the glow, and the caret is
        // asserted on the plain composer scene above.
        if (!(await screenshotWithCaret(page, { path: `${OUT}/composer-${theme}-approval-focused-crop.png`, clip: approvalClip }, ta))) console.log(`chat/${theme}/approval: no caret caught within the attempt window; frame taken without it`)
      } else {
        console.log(`chat/${theme}/approval: textarea does not hold focus while a decision is pending; plain frame`)
        await page.screenshot({ path: `${OUT}/composer-${theme}-approval-focused-crop.png`, clip: approvalClip })
      }
    }
    await page.screenshot({ path: `${OUT}/composer-${theme}${variant ? '-' + variant : ''}.png` })
    const cropOpts = {
      path: `${OUT}/composer-${theme}${variant ? '-' + variant : ''}-crop.png`,
      clip: { x: Math.max(0, box.x - 40), y: Math.max(0, box.y - 260), width: box.width + 80, height: box.height + 300 },
    }
    if (!variant) {
      // The textarea holds focus here and the caret is the only indicator, so
      // the frame must show it.
      if (!(await screenshotWithCaret(page, cropOpts, page.getByLabel('Message input').first()))) throw new Error(`chat/${theme}: no caret caught in the focused composer frame`)
    } else {
      await page.screenshot(cropOpts)
    }
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
    await assertGlass(page, search.locator('xpath=ancestor::*[contains(@class,"glass-shadow")][1]'), `settings/${theme}`)
    const halo = search.locator('xpath=ancestor::*[contains(@class,"glass-shadow")][1]')
    const restShadow = await halo.evaluate(el => getComputedStyle(el).boxShadow)
    const restEdge = await halo.evaluate(el => getComputedStyle(el).getPropertyValue('--glass-edge').trim())
    await page.screenshot({ path: `${OUT}/settings-capsule-${theme}.png` })
    console.log('wrote', `${OUT}/settings-capsule-${theme}.png`)
    // Focused: the capsule does not change (maintainer decision) -- same
    // shadow, same side lines, no accent, no outline on the input; the caret is
    // the indicator. Assert the pane is untouched by focus.
    await search.tap()
    await page.waitForTimeout(400)
    const focused = await search.evaluate(el => document.activeElement === el)
    if (!focused) throw new Error(`settings/${theme}: search input did not take focus`)
    const focusShadow = await halo.evaluate(el => getComputedStyle(el).boxShadow)
    if (focusShadow !== restShadow) throw new Error(`settings/${theme}: capsule shadow changed on focus (${restShadow} -> ${focusShadow})`)
    const focusEdge = await halo.evaluate(el => getComputedStyle(el).getPropertyValue('--glass-edge').trim())
    if (focusEdge !== restEdge) throw new Error(`settings/${theme}: side line changed on focus (${restEdge} -> ${focusEdge})`)
    console.log(`settings/${theme}: focus leaves the capsule unchanged -- shadow ${focusShadow}; edge ${focusEdge}`)
    await page.screenshot({ path: `${OUT}/settings-capsule-${theme}-focused.png` })
    console.log('wrote', `${OUT}/settings-capsule-${theme}-focused.png`)
    // The capsule up close, rest and focused, so the caret -- the only thing
    // that differs -- is visible to a reader of the attachment (a 3x phone
    // frame shrinks it to a hairline).
    const cbox = await halo.boundingBox()
    const cclip = { x: Math.max(0, cbox.x - 24), y: Math.max(0, cbox.y - 24), width: cbox.width + 48, height: cbox.height + 48 }
    if (!(await screenshotWithCaret(page, { path: `${OUT}/settings-capsule-${theme}-focused-crop.png`, clip: cclip }, search))) throw new Error(`settings/${theme}: no caret caught in the focused capsule frame`)
    console.log('wrote', `${OUT}/settings-capsule-${theme}-focused-crop.png`)
    await search.evaluate(el => el.blur())
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/settings-capsule-${theme}-rest-crop.png`, clip: cclip })
    console.log('wrote', `${OUT}/settings-capsule-${theme}-rest-crop.png`)
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
    // The desktop search bar is a boxed input, not a glass pane, so it keeps
    // a focus ring: the shared `focus-ring` shape, but a darker border and a
    // soft neutral halo, never the theme accent. Focus via the keyboard so :focus-visible matches
    // the way it does for a typing affordance, then read the computed ring.
    const search = page.locator('.settings-search input').first()
    if (!(await search.count())) throw new Error(`settings-desktop/${theme}: search bar missing`)
    const accent = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim())
    const restRing = await search.evaluate(el => ({ border: getComputedStyle(el).borderTopColor, shadow: getComputedStyle(el).boxShadow }))
    await search.focus()
    await page.waitForTimeout(300)
    const focusRing = await search.evaluate(el => ({ border: getComputedStyle(el).borderTopColor, shadow: getComputedStyle(el).boxShadow }))
    const accentRgb = await page.evaluate(c => { const d = document.createElement('div'); d.style.color = c; document.body.appendChild(d); const v = getComputedStyle(d).color; d.remove(); return v }, accent)
    if (focusRing.border === restRing.border && focusRing.shadow === restRing.shadow) throw new Error(`settings-desktop/${theme}: search bar shows no focus cue`)
    if (focusRing.border === accentRgb || focusRing.shadow.includes(accentRgb.replace(/^rgb\(([^)]*)\)$/, 'rgba($1'))) throw new Error(`settings-desktop/${theme}: search bar focus is the accent (${focusRing.border}; ${focusRing.shadow})`)
    console.log(`settings-desktop/${theme}: search focus border ${restRing.border} -> ${focusRing.border}; shadow ${focusRing.shadow}`)
    const sbox = await search.boundingBox()
    await page.screenshot({
      path: `${OUT}/settings-desktop-${theme}-search-focused-crop.png`,
      clip: { x: Math.max(0, sbox.x - 24), y: Math.max(0, sbox.y - 24), width: sbox.width + 48, height: sbox.height + 48 },
    })
    console.log('wrote', `${OUT}/settings-desktop-${theme}-search-focused-crop.png`)
    // The switch itself (Settings -> Display -> Theme card): photograph the row
    // off, flip it, assert the root attribute and the stored key follow, and
    // photograph it on. The switch is the only way a user reaches the solid
    // rendering without an OS setting, so the row must be findable and work.
    const row = page.locator('[data-setting-label="Reduce glass transparency"]').first()
    if (!(await row.count())) throw new Error(`settings-desktop/${theme}: "Reduce glass transparency" row missing`)
    await row.scrollIntoViewIfNeeded()
    await page.waitForTimeout(300)
    const readSwitch = () => page.evaluate(() => ({ html: document.documentElement.dataset.reduceTransparency ?? '', stored: localStorage.getItem('mc-reduce-transparency') }))
    const off = await readSwitch()
    if (off.html === 'on' || off.stored === 'on') throw new Error(`settings-desktop/${theme}: switch already on before the click (${JSON.stringify(off)})`)
    const card = row.locator('xpath=ancestor::*[contains(concat(" ", normalize-space(@class), " "), " card-glow ")][1]')
    const target = (await card.count()) ? card : row
    const rbox = await target.boundingBox()
    const clip = { x: Math.max(0, rbox.x - 16), y: Math.max(0, rbox.y - 16), width: rbox.width + 32, height: rbox.height + 32 }
    await page.screenshot({ path: `${OUT}/settings-desktop-${theme}-reduce-toggle-off-crop.png`, clip })
    console.log('wrote', `${OUT}/settings-desktop-${theme}-reduce-toggle-off-crop.png`)
    await row.getByRole('switch').first().click()
    await page.waitForTimeout(300)
    const on = await readSwitch()
    if (on.html !== 'on') throw new Error(`settings-desktop/${theme}: switch did not set data-reduce-transparency (${on.html})`)
    if (on.stored !== 'on') throw new Error(`settings-desktop/${theme}: switch did not persist mc-reduce-transparency (${on.stored})`)
    await page.screenshot({ path: `${OUT}/settings-desktop-${theme}-reduce-toggle-on-crop.png`, clip })
    console.log(`settings-desktop/${theme}: reduce-transparency switch off -> on (root attribute + stored key follow)`)
    console.log('wrote', `${OUT}/settings-desktop-${theme}-reduce-toggle-on-crop.png`)
    await context.close()
  }

  /** Records the approval band's transitions as one GIF: tool bar + withheld
   *  spawn card -> the tool decision lands (the bar now shows the spawn's own
   *  request; the card stays withheld, since that request IS a permission row)
   *  -> the user sends a message (the bar hides; the card fades its Approve /
   *  Reject in). Each step is asserted before the next frame is pushed. */
  async function spawnFlow(theme) {
    activeDetail = approvalDetail
    const videoDir = `${OUT}/.video-${theme}`
    const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 1, recordVideo: { dir: videoDir, size: { width: 1500, height: 950 } } })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { slots, theme, extra })
    let sock = null
    await page.routeWebSocket(/\/api\/ws/, ws => { sock = ws })
    await page.addInitScript(slot => { localStorage.setItem('mc-active-slot', slot) }, SLOT)
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
    if (!sock) throw new Error(`spawn-flow/${theme}: websocket never opened`)
    const card = page.getByTestId('spawn-approval-card')
    const bar = page.getByRole('button', { name: /allow once/i })
    if (!(await bar.count())) throw new Error(`spawn-flow/${theme}: tool approval bar missing at start`)
    // Hold the settled rest state for a beat so the GIF opens on it (the
    // recording is trimmed to this point below).
    await page.waitForTimeout(800)
    // 1. the spawn request arrives beside the pending tool approval
    sock.send(JSON.stringify(spawnFrame))
    await page.waitForTimeout(1500)
    if (!(await page.getByText(/^1 sub-agent pending — answer the request below first$/).count())) throw new Error(`spawn-flow/${theme}: withheld card missing after the spawn frame`)
    if (await card.getByRole('button', { name: /^Approve$/ }).count()) throw new Error(`spawn-flow/${theme}: card offers Approve beside the tool bar`)
    // 2. the tool decision lands: the bar now carries the spawn's own request
    sock.send(JSON.stringify(toolResolvedFrame))
    await page.waitForTimeout(1800)
    if (!(await bar.count())) throw new Error(`spawn-flow/${theme}: bar vanished on the tool decision -- the spawn's own permission row should hold it`)
    if (!(await page.getByText(/spawn_run\(/).count())) throw new Error(`spawn-flow/${theme}: bar does not show the spawn request after the tool decision`)
    if (await card.getByRole('button', { name: /^Approve$/ }).count()) throw new Error(`spawn-flow/${theme}: card went live while its request is on the bar`)
    // 3. the user speaks: the bar hides, the card takes the decision
    sock.send(JSON.stringify(userFrame))
    await page.waitForTimeout(1800)
    if (await bar.count()) throw new Error(`spawn-flow/${theme}: bar still up after the user message`)
    if (!(await card.getByRole('button', { name: /^Approve$/ }).count())) throw new Error(`spawn-flow/${theme}: card did not go live after the user message`)
    if (!(await page.getByText(/1 sub-agent is awaiting your approval to run/).count())) throw new Error(`spawn-flow/${theme}: live copy missing`)
    await page.waitForTimeout(1200)
    const video = page.video()
    await context.close()
    const webm = await video.path()
    const gif = `${OUT}/spawn-flow-${theme}.gif`
    // Crop to the dock band (bottom 420px of the 950px viewport) so the GIF stays
    // small enough for a PR attachment; 10 fps is plenty for a 150ms fade.
    // `-ss` drops the recording's first 2.6s: the context records from before
    // `goto`, so those frames are the blank page and the un-themed load (a
    // reviewer opened the earlier GIFs on a white rectangle) — the 2500ms
    // settle plus part of the hold above, so the first kept frame is the
    // themed, settled band.
    const ff = spawnSync('ffmpeg', ['-y', '-loglevel', 'error', '-ss', '2.6', '-i', webm,
      '-vf', 'crop=1500:420:0:530,fps=10,scale=1100:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer:bayer_scale=3',
      gif], { stdio: 'inherit' })
    if (ff.status !== 0) throw new Error(`spawn-flow/${theme}: ffmpeg failed (${ff.status})`)
    try { unlinkSync(webm) } catch { /* video already gone */ }
    try { renameSync(videoDir, `${OUT}/.video-${theme}-done`) } catch { /* leave it */ }
    console.log('wrote', gif)
  }

  for (const theme of ['dark', 'light']) {
    await chat(theme)
    await chat(theme, 'long')
    await chat(theme, 'approval')
    await chat(theme, 'incognito')
    await chat(theme, 'chips')
    await chat(theme, 'chips-picked')
    await chat(theme, 'welcome')
    await chat(theme, 'question')
    await chat(theme, 'folder')
    await chat(theme, 'spawn')
    await chat(theme, 'spawn-both')
    await chat(theme, 'tip')
    await chat(theme, 'collapsed')
    await chat(theme, 'reduce')
    await chat(theme, 'reduce-long')
    await settingsMobile(theme)
    await settingsDesktop(theme)
    await spawnFlow(theme)
  }

  await browser.close()
  srv.close()
  console.log('OK')
}

main().catch(err => { console.error(err); process.exit(1) })
