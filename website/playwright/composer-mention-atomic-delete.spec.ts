import { test, expect, type Page } from '@playwright/test'

/**
 * #18263: deleting part of a staged `@mention` through the textarea's
 * value-change seam (cut, type-over, paste-over, drop) must remove the WHOLE
 * mention — never leave a half-reference whose chip then silently unstages.
 *
 * The seam is handled in a native `beforeinput` listener (ChatInput /
 * chat-input/keyboard.ts), so the unit tests (happy-dom) dispatch synthetic
 * InputEvents. This spec proves the behaviour against a REAL browser engine
 * (Chromium) where the events are produced by actual keyboard, paste and cut:
 * the Design review required real-engine evidence that `beforeinput` carries
 * the data/clipboard the handler reads. The file picker's search is mocked so
 * the spec does not depend on files existing on the gateway host; everything
 * else — the pick, the staging, the edits — is the real app path.
 */

const MENTION = '@src/main.ts'

/** Create a slot, point it at a project, mock file search, open it, and stage a
 *  file mention through the real picker. Returns the composer textarea. */
async function stageMention(page: Page): Promise<{ ta: ReturnType<Page['locator']>; slot: string }> {
  // The @-file picker is gated on the slot having a PROJECT directory, and the
  // gateway rejects a project path that is not a real directory on its host
  // ("Not a directory"). So resolve a directory that actually exists: prefer a
  // recent project the gateway already knows, else `/tmp` (present on the CI
  // Linux host). The mocked file search then reports that same dir as its root,
  // so the picked file's relative mention is `@src/main.ts` under it. Only the
  // file SEARCH is mocked — the pick, staging and the edits are the real path.
  const recent = (await page.request.get('/api/recent-projects').then(r => r.ok() ? r.json() : { dirs: [] }).catch(() => ({ dirs: [] }))) as { dirs?: string[] }
  const project = recent.dirs?.[0] || '/tmp'
  await page.route('**/api/file-search*', route =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        root: project,
        results: [{ path: `${project.replace(/\/$/, '')}/src/main.ts`, name: 'main.ts', size: 10, mtime: Math.floor(Date.now() / 1000), kind: 'file' }],
      }),
    }))
  const created = await page.request.post('/api/chat/slots', { data: { agent: 'default' } })
  expect(created.ok(), await created.text()).toBeTruthy()
  const slot = ((await created.json()) as { key: string }).key
  const proj = await page.request.post(`/api/chat/slots/${encodeURIComponent(slot)}/project`, { data: { project } })
  expect(proj.ok(), `set project ${project}: ${proj.status()} ${await proj.text()}`).toBeTruthy()
  await page.goto(`/chat?sid=${encodeURIComponent(slot)}`, { waitUntil: 'domcontentloaded' })
  const ta = page.locator('textarea[data-composer-input]').first()
  await expect(ta).toBeVisible({ timeout: 15000 })
  await ta.click()
  await page.keyboard.type('@mai')
  // The real picker lists the mocked result; pick it (mousedown, as the menu uses).
  const option = page.getByRole('option').filter({ hasText: 'main.ts' }).first()
  await expect(option).toBeVisible({ timeout: 5000 })
  await option.dispatchEvent('mousedown')
  await expect(ta).toHaveValue(new RegExp(MENTION.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')), { timeout: 5000 })
  // The staged file renders a chip group labelled with its path; match the
  // basename so it is robust to the strip's hidden measurement duplicate.
  await expect(page.getByRole('group', { name: /main\.ts/ }).first()).toBeVisible({ timeout: 5000 })
  return { ta, slot }
}

/** Set the textarea value + caret to a known starting point, then a selection. */
async function setDraftAndSelect(ta: ReturnType<Page['locator']>, value: string, selStart: number, selEnd: number) {
  await ta.evaluate((el: HTMLTextAreaElement, v: { value: string; s: number; e: number }) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!
    setter.call(el, v.value)
    el.dispatchEvent(new Event('input', { bubbles: true }))
    el.setSelectionRange(v.s, v.e)
    el.focus()
  }, { value, s: selStart, e: selEnd })
}

/** A REAL paste: write the clipboard, then press the paste shortcut so the
 *  browser produces a trusted `insertFromPaste` beforeinput (a synthetic
 *  ClipboardEvent is untrusted and triggers no default edit). */
async function pasteOver(page: Page, text: string) {
  await page.evaluate(t => navigator.clipboard.writeText(t), text)
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+v' : 'Control+v')
}

test.describe('#18263 atomic @mention delete through the value-change seam', () => {
  // Real paste/cut need clipboard access in Chromium.
  test.use({ permissions: ['clipboard-read', 'clipboard-write'] })

  test.afterEach(async ({ page }) => {
    const slot = (test.info() as unknown as { _mentionSlot?: string })._mentionSlot
    if (slot) await page.request.delete(`/api/chat/slots/${encodeURIComponent(slot)}`).catch(() => {})
  })

  test('typing over a selection that overlaps the mention removes it whole', async ({ page }) => {
    const { ta, slot } = await stageMention(page)
    ;(test.info() as unknown as { _mentionSlot?: string })._mentionSlot = slot
    await setDraftAndSelect(ta, `review ${MENTION} now`, 'review @src/'.length, `review ${MENTION}`.length)
    await page.keyboard.type('x')
    await expect(ta).toHaveValue('review x now')
    await expect(ta).not.toHaveValue(/@src\//)
    await expect(page.getByRole('group', { name: /main\.ts/ })).toHaveCount(0)
  })

  test('pasting over a selection that overlaps the mention removes it whole', async ({ page }) => {
    const { ta, slot } = await stageMention(page)
    ;(test.info() as unknown as { _mentionSlot?: string })._mentionSlot = slot
    await setDraftAndSelect(ta, `review ${MENTION} now`, 'review @src/'.length, `review ${MENTION}`.length)
    await pasteOver(page, 'README.md')
    await expect(ta).toHaveValue('review README.md now')
    await expect(ta).not.toHaveValue(/@src\//)
    await expect(page.getByRole('group', { name: /main\.ts/ })).toHaveCount(0)
  })

  test('a paste sharing the mention prefix keeps the whole paste (no data loss)', async ({ page }) => {
    const { ta, slot } = await stageMention(page)
    ;(test.info() as unknown as { _mentionSlot?: string })._mentionSlot = slot
    await setDraftAndSelect(ta, `review ${MENTION} now`, 'review @src/'.length, `review ${MENTION}`.length)
    await pasteOver(page, 'main.py')
    await expect(ta).toHaveValue('review main.py now')
  })

  test('cutting a selection that overlaps the mention removes it whole and keeps the clipboard', async ({ page }) => {
    const { ta, slot } = await stageMention(page)
    ;(test.info() as unknown as { _mentionSlot?: string })._mentionSlot = slot
    await setDraftAndSelect(ta, `review ${MENTION} now`, 'review @src/'.length, `review ${MENTION} `.length)
    await page.keyboard.press(process.platform === 'darwin' ? 'Meta+x' : 'Control+x')
    // Whole mention removed; no `@src/` fragment; chip unstaged.
    await expect(ta).not.toHaveValue(/@src\//)
    await expect(page.getByRole('group', { name: /main\.ts/ })).toHaveCount(0)
    // The browser's own cut still wrote the selected text to the clipboard.
    const clip = await page.evaluate(() => navigator.clipboard.readText().catch(() => ''))
    expect(clip).toContain('main.ts')
  })
})
