/** Verify inline terminal renaming, its focus cues and the F2 hint in the built SPA.
 * Gateway-free: API responses and terminal WebSockets are synthetic fixtures.
 * Usage: node scripts/capture-terminal-rename.mjs [output-directory]
 *
 * Writes, per theme: `<theme>-editing.png`, `<theme>-context-menu.png` (the
 * Rename item with its F2 hint), `<theme>-{640,320}-focus.png`, `<theme>-popout.png`,
 * plus one `rename-save-cancel.webm` recording of label -> editor -> label for
 * a save and for a cancel. Every frame is written only after the assertions
 * that describe it have passed, so a frame cannot photograph a state the code
 * does not produce.
 */
import { chromium, expect } from '@playwright/test'
import { mkdirSync, renameSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const out = process.argv[2] || join(process.env.KIROCREW_SCRATCH || 'temp-screenshots', 'terminal-rename')
mkdirSync(out, { recursive: true })
const { srv, base } = await serveDist()
const tabs = [1, 2, 3, 4].map(n => ({ id: `fixture-terminal-${n}`, cwd: '/workspace' }))

/** Stub the API, seed four tabs and answer their WebSockets with a title + ready. */
async function preparePage(page, theme) {
  const errors = []
  page.on('pageerror', e => errors.push(e.message))
  await stubDashboardApi(page, {
    theme, preserveStorage: true,
    extra: async (path, route) => {
      if (path === '/api/terminal/sessions') {
        await json(route, { enabled: true, sessions: tabs.map(t => ({ session_id: t.id, alive: true })) })
        return true
      }
      if (path === '/api/chat/slots' && route.request().method() === 'POST') {
        await json(route, { key: 'fixture-chat', title: 'New Session', agent: 'kirocrew' })
        return true
      }
      return false
    },
  })
  // Seed once: reloads must restore the value written by the real rename action.
  await page.addInitScript(({ tabs }) => {
    if (!localStorage.getItem('mc-bottom-terminal')) localStorage.setItem('mc-bottom-terminal', JSON.stringify({ open: true, height: 320, tabs, activeId: tabs[0].id }))
  }, { tabs })
  const sockets = []
  const counts = { connections: 0, inputMessages: 0 }
  await page.routeWebSocket(/\/api\/ws\/terminal\//, ws => {
    counts.connections++
    sockets.push(ws)
    ws.send(JSON.stringify({ type: 'title', text: 'workspace' }))
    ws.send(JSON.stringify({ type: 'ready' }))
    ws.send(Buffer.from('$ Ready for work\r\n'))
    ws.onMessage(message => { if (typeof message !== 'string') counts.inputMessages++ })
  })
  return { errors, sockets, counts }
}

/**
 * Measure the painted keyboard-focus cue of a chip against the strip's
 * scrollport, in the browser. One geometry, two colours, chosen by selection
 * state — both a 2px solid outline offset 2px, so the outer edge sits 4px out
 * and must stay inside the strip's 4px gutter:
 *  - the SELECTED pill keeps the global outline in `--accent`;
 *  - an INACTIVE chip paints it in NEUTRAL `--muted`, which must also differ
 *    from `--accent` and from `--ring` — an accent ring beside the selected pill
 *    reads as a second selection at any strength, so the cue differs in colour.
 * Also reports the WCAG non-text contrast of the outline colour against the
 * panel background, so "complete" is a number rather than a claim.
 */
const measureFocusCue = el => {
  const r = el.getBoundingClientRect()
  const parent = el.closest('[role=tablist]').getBoundingClientRect()
  const css = getComputedStyle(el)
  const selected = el.getAttribute('aria-selected') === 'true'
  const resolve = token => {
    const probe = document.createElement('span')
    probe.style.color = `var(${token})`
    document.body.appendChild(probe)
    const value = getComputedStyle(probe).color
    probe.remove()
    return value
  }
  const luminance = rgb => {
    const [r, g, b] = rgb.match(/[\d.]+/g).slice(0, 3).map(Number).map(c => {
      const s = c / 255
      return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
    })
    return 0.2126 * r + 0.7152 * g + 0.0722 * b
  }
  const contrast = (a, b) => {
    const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
    return (hi + 0.05) / (lo + 0.05)
  }
  const extent = parseFloat(css.outlineWidth) + parseFloat(css.outlineOffset)
  const contained = extent === 4 && r.left - extent >= parent.left - 0.1 && r.right + extent <= parent.right + 0.1
    && r.top - extent >= parent.top - 0.1 && r.bottom + extent <= parent.bottom + 0.1
  const accent = resolve('--accent'), ring = resolve('--ring'), muted = resolve('--muted')
  const solid = css.outlineStyle === 'solid' && parseFloat(css.outlineWidth) === 2
  if (selected) {
    return { selected, cue: 'accent-outline', color: css.outlineColor, ok: solid && css.outlineColor === accent && contained, contrast: contrast(css.outlineColor, resolve('--bg')) }
  }
  // The outline is the WHOLE cue: no shadow ring may accompany it (that is
  // the `.focus-ring` glow, whose `--accent-subtle` is selection colour).
  return {
    selected, cue: 'muted-outline', color: css.outlineColor,
    ok: solid && css.boxShadow === 'none' && css.outlineColor === muted && css.outlineColor !== accent && css.outlineColor !== ring && contained,
    contrast: contrast(css.outlineColor, resolve('--bg')),
  }
}

/** Focus `chip` from the keyboard (F2 then Escape leaves it :focus-visible) and assert its cue. */
async function expectFocusCue(chip, input, expectedCue) {
  await chip.focus()
  await chip.press('F2')
  await expect(input).toBeFocused()
  await input.press('Escape')
  await expect(chip).toBeFocused()
  // Poll: the chip's transition-colors is mid-fade on the first frame after focus.
  let cue
  await expect.poll(async () => { cue = await chip.evaluate(measureFocusCue); return cue.ok && cue.cue === expectedCue }).toBe(true)
  // Non-text contrast floor (WCAG 1.4.11) for the cue's line colour.
  expect(cue.contrast).toBeGreaterThanOrEqual(3)
  return cue
}

let browser
const results = []
try {
  browser = await chromium.launch({ headless: true })
  for (const theme of ['dark', 'light']) {
    const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 1 })
    const page = await context.newPage()
    page.setDefaultTimeout(12000)
    const { errors, sockets, counts } = await preparePage(page, theme)
    await page.goto(base + '/chat')
    const tablist = page.getByRole('tablist').filter({ has: page.getByRole('tab', { name: 'workspace', exact: true }) })
    await expect(tablist.getByRole('tab')).toHaveCount(4)
    const first = page.getByRole('tab', { name: 'workspace', exact: true }).first()
    await expect(first).toBeVisible()
    const initialConnections = counts.connections
    await first.dblclick()
    const input = page.getByRole('textbox', { name: 'Terminal name', exact: true })
    await expect(input).toBeFocused()
    await input.fill('Build logs')
    await expect(page.getByTestId('terminal-rename-hint')).toBeVisible()
    // The chip fades its selected pill out (transition-colors) as the field
    // takes over; a frame shot mid-fade shows both and misreads as a ring.
    await expect.poll(() => first.evaluate(el => getComputedStyle(el).backgroundColor)).toBe('rgba(0, 0, 0, 0)')
    await page.screenshot({ path: join(out, `${theme}-editing.png`) })
    await input.press('Enter')
    await expect(page.getByTestId('terminal-rename-hint')).toBeHidden()
    const renamed = page.getByRole('tab', { name: 'Build logs', exact: true })
    await expect(renamed).toBeFocused()
    await renamed.press('F2')
    await expect(input).toBeFocused()
    await input.fill('Discard this')
    await input.press('Escape')
    await expect(renamed).toBeFocused()
    await renamed.click({ button: 'right' })
    // Sighted discovery of F2: the Rename item shows the key, declares it to
    // AT on its own aria-keyshortcuts, and keeps its accessible name "Rename"
    // (the exact-name query is what fails if the badge leaks into the name).
    const renameItem = page.getByRole('menuitem', { name: 'Rename', exact: true })
    await expect(renameItem).toBeVisible()
    await expect(renameItem).toHaveAttribute('aria-keyshortcuts', 'F2')
    const shortcut = renameItem.getByTestId('terminal-rename-shortcut')
    await expect(shortcut).toBeVisible()
    await expect(shortcut).toHaveText('F2')
    await expect(shortcut).toHaveAttribute('aria-hidden', 'true')
    // The badge sits inside the item's box, flush right, and does not paint in
    // the item's own text colour (it is a hint, not a second label).
    expect(await shortcut.evaluate((el, item) => {
      const s = el.getBoundingClientRect(), i = item.getBoundingClientRect()
      return s.right <= i.right + 0.1 && s.left > i.left + i.width / 2 && s.top >= i.top - 0.1 && s.bottom <= i.bottom + 0.1
        && getComputedStyle(el).color !== getComputedStyle(item).color
    }, await renameItem.elementHandle())).toBe(true)
    await page.screenshot({ path: join(out, `${theme}-context-menu.png`) })
    await renameItem.click()
    await expect(input).toBeFocused()
    await input.fill('Review shell')
    await input.press('Enter')
    const review = page.getByRole('tab', { name: 'Review shell', exact: true })
    await expect(review).toBeFocused()
    for (const ws of sockets) ws.send(JSON.stringify({ type: 'title', text: 'npm run build' }))
    await expect(review).toBeVisible()
    await expect(page.getByRole('tab', { name: 'npm run build', exact: true })).toHaveCount(3)
    expect(counts.connections).toBe(initialConnections)
    expect(counts.inputMessages).toBe(0)
    await page.reload()
    await expect(page.getByRole('tab', { name: 'Review shell', exact: true })).toBeVisible()
    await page.getByRole('tab', { name: 'Review shell', exact: true }).click({ button: 'right' })
    await page.getByRole('menuitem', { name: 'Use automatic name', exact: true }).click()
    await expect(page.getByRole('tab', { name: 'workspace', exact: true })).toHaveCount(4)

    for (const width of [1400, 640, 390, 320]) {
      await page.setViewportSize({ width, height: 900 })
      // Tab 0 is the selected pill; tab 3 is inactive — the two cue vocabularies.
      for (const [index, expectedCue] of [[0, 'accent-outline'], [3, 'muted-outline']]) {
        const chip = tablist.getByRole('tab').nth(index)
        await expect(chip).toHaveAttribute('aria-selected', index === 0 ? 'true' : 'false')
        const cue = await expectFocusCue(chip, input, expectedCue)
        results.push(`${theme} ${width}px ${index === 0 ? 'selected' : 'inactive'} tab: ${cue.cue} ${cue.color} contained, ${cue.contrast.toFixed(2)}:1 on --bg`)
      }
      if (width === 640 || width === 320) await page.screenshot({ path: join(out, `${theme}-${width}-focus.png`) })
    }

    // Exercise the real popout route and its shared persisted tab store, without
    // claiming coverage of native window management or a real PTY takeover.
    await page.setViewportSize({ width: 900, height: 600 })
    await page.goto(base + '/popout/terminal')
    const popoutTab = page.getByRole('tab').last()
    await expect(popoutTab).toBeVisible()
    await expect(popoutTab).toHaveAttribute('aria-selected', 'false')
    await popoutTab.click({ button: 'right' })
    await page.getByRole('menuitem', { name: 'Rename', exact: true }).click()
    await expect(input).toBeFocused()
    await input.fill('Popout logs')
    await input.press('Enter')
    const savedPopoutTab = page.getByRole('tab', { name: 'Popout logs', exact: true })
    await expect(savedPopoutTab).toBeFocused()
    // Focus returned to a chip that is NOT selected: the neutral outline, not
    // an accent ring, beside the popout's selected pill.
    await expect.poll(() => savedPopoutTab.evaluate(measureFocusCue).then(c => c.ok && c.cue === 'muted-outline')).toBe(true)
    await page.screenshot({ path: join(out, `${theme}-popout.png`) })
    await page.goto(base + '/chat')
    await expect(page.getByRole('tab', { name: 'Popout logs', exact: true })).toBeVisible()
    expect(errors).toEqual([])
    results.push(`${theme}: double-click, F2, context menu with F2 hint, save, cancel, reload, live-title override/reset, popout persistence, no terminal input or reconnect on rename: PASS`)
    await context.close()
  }

  // One recording of the label <-> editor round trip, for a save and for a
  // cancel, on an INACTIVE tab so the final frame shows the neutral focus cue
  // beside the selected pill. Playwright finalizes the file on context close.
  {
    const size = { width: 900, height: 420 }
    const context = await browser.newContext({ viewport: size, deviceScaleFactor: 1, recordVideo: { dir: out, size } })
    const page = await context.newPage()
    page.setDefaultTimeout(12000)
    const { errors } = await preparePage(page, 'dark')
    await page.goto(base + '/chat')
    const tablist = page.getByRole('tablist').filter({ has: page.getByRole('tab', { name: 'workspace', exact: true }) })
    await expect(tablist.getByRole('tab')).toHaveCount(4)
    const chip = tablist.getByRole('tab').nth(3)
    const input = page.getByRole('textbox', { name: 'Terminal name', exact: true })
    const settle = () => page.waitForTimeout(700)
    await chip.focus()
    await settle()
    await chip.press('F2')
    await expect(input).toBeFocused()
    await settle()
    await input.pressSequentially('Build logs', { delay: 60 })
    await settle()
    await input.press('Enter')
    const saved = page.getByRole('tab', { name: 'Build logs', exact: true })
    await expect(saved).toBeFocused()
    await expect(saved).toHaveAttribute('aria-selected', 'false')
    await expect.poll(() => saved.evaluate(measureFocusCue).then(c => c.ok && c.cue === 'muted-outline')).toBe(true)
    await settle()
    await saved.press('F2')
    await expect(input).toBeFocused()
    await settle()
    await input.pressSequentially('Discarded', { delay: 60 })
    await settle()
    await input.press('Escape')
    await expect(saved).toBeFocused()
    await expect(saved).toHaveAccessibleName('Build logs')
    await settle()
    expect(errors).toEqual([])
    const video = page.video()
    await context.close()
    renameSync(await video.path(), join(out, 'rename-save-cancel.webm'))
    results.push('recording: inactive tab F2 -> type -> Enter (saved) and F2 -> type -> Escape (cancelled), focus back on the neutral outline: PASS')
  }
  console.log(results.join('\n'))
  console.log('Artifacts: ' + out)
} finally {
  await browser?.close()
  await new Promise(resolve => srv.close(resolve))
}
