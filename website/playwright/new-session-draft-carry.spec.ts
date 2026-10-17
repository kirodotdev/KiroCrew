import { test, expect } from '@playwright/test'
import type { Page } from '@playwright/test'

/**
 * Text typed while a new session is still being created must land in that new
 * session instead of vanishing when it activates.
 *
 * The single-chat surface has one composer, bound to the ACTIVE slot. A create
 * leaves the old slot active until the POST resolves, so a keystroke in that
 * window writes the old slot's draft; the activation then restores the new
 * slot's (empty) draft over the composer and the text disappears from view.
 * The POST is delayed here so the window is wide enough to type into on every
 * runner, not just a slow one.
 */

const CREATE_DELAY_MS = 1500

async function delaySlotCreate(page: Page, ms = CREATE_DELAY_MS) {
  await page.route('**/api/chat/slots', async route => {
    if (route.request().method() !== 'POST') return route.fallback()
    await new Promise(resolve => setTimeout(resolve, ms))
    await route.fallback()
  })
}

function waitForCreate(page: Page) {
  return page.waitForResponse(response =>
    response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/chat/slots',
  )
}

async function openSeededSession(page: Page): Promise<string> {
  const origin = await (await page.request.post('/api/chat/slots', { data: { agent: 'default' } })).json() as { key: string }
  await page.goto(`/chat?sid=${encodeURIComponent(origin.key)}`, { waitUntil: 'domcontentloaded' })
  await expect(page).toHaveURL(url => url.searchParams.get('sid') === origin.key)
  await expect(page.locator('textarea[data-composer-input]')).toBeVisible({ timeout: 10000 })
  return origin.key
}

test.describe('New session keeps text typed while it is being created', () => {
  test('keyboard shortcut: typing during the create lands in the new session', async ({ page }) => {
    const originKey = await openSeededSession(page)
    const composer = page.locator('textarea[data-composer-input]')
    await delaySlotCreate(page)

    await composer.click()
    const created = waitForCreate(page)
    await page.keyboard.press('Alt+Shift+N')
    // The caret is still in the composer: type straight away, as a fast typist does.
    await page.keyboard.type('typed while creating')
    const { key } = await (await created).json() as { key: string }
    expect(key).not.toBe(originKey)
    await expect(page).toHaveURL(url => url.searchParams.get('sid') === key)

    await expect(composer).toHaveValue('typed while creating')
    // Nothing leaks back into the session the user left.
    await page.goto(`/chat?sid=${encodeURIComponent(originKey)}`, { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveURL(url => url.searchParams.get('sid') === originKey)
    await expect(composer).toBeVisible({ timeout: 10000 })
    await expect(composer).toHaveValue('')
  })

  test('new-chat button: clicking into the composer and typing during the create', async ({ page }) => {
    const originKey = await openSeededSession(page)
    const composer = page.locator('textarea[data-composer-input]')
    await delaySlotCreate(page)

    const created = waitForCreate(page)
    await page.getByRole('button', { name: 'New chat session', exact: true }).click()
    await composer.click()
    await page.keyboard.type('hello new session')
    const { key } = await (await created).json() as { key: string }
    expect(key).not.toBe(originKey)
    await expect(page).toHaveURL(url => url.searchParams.get('sid') === key)

    await expect(composer).toHaveValue('hello new session')
  })

  test('a background create in flight does not make text typed before the shortcut move', async ({ page }) => {
    const originKey = await openSeededSession(page)
    const composer = page.locator('textarea[data-composer-input]')
    await delaySlotCreate(page)

    // Middle-click New: a BACKGROUND create, which keeps focus on this session.
    const createdKeys: string[] = []
    page.on('response', async response => {
      if (response.request().method() !== 'POST' || new URL(response.url()).pathname !== '/api/chat/slots') return
      createdKeys.push(((await response.json()) as { key: string }).key)
    })
    await page.getByRole('button', { name: 'New chat session', exact: true }).click({ button: 'middle' })
    await composer.click()
    await page.keyboard.type('typed before the shortcut')
    // Then a foreground create while the background one is still pending.
    await page.keyboard.press('Alt+Shift+N')
    await page.keyboard.type('after')
    await expect.poll(() => createdKeys.length, { timeout: 10000 }).toBe(2)
    const fgKey = createdKeys[1]
    await expect(page).toHaveURL(url => url.searchParams.get('sid') === fgKey)

    await expect(composer).toHaveValue('after')
    await page.goto(`/chat?sid=${encodeURIComponent(originKey)}`, { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveURL(url => url.searchParams.get('sid') === originKey)
    await expect(composer).toBeVisible({ timeout: 10000 })
    await expect(composer).toHaveValue('typed before the shortcut')
  })

  test('two quick shortcut presses: the text lands in the session that opened', async ({ page }) => {
    await openSeededSession(page)
    const composer = page.locator('textarea[data-composer-input]')
    await delaySlotCreate(page)

    const createdKeys: string[] = []
    page.on('response', async response => {
      if (response.request().method() !== 'POST' || new URL(response.url()).pathname !== '/api/chat/slots') return
      createdKeys.push(((await response.json()) as { key: string }).key)
    })
    await composer.click()
    await page.keyboard.press('Alt+Shift+N')
    await page.keyboard.press('Alt+Shift+N')
    await page.keyboard.type('typed after two presses')
    await expect.poll(() => createdKeys.length, { timeout: 10000 }).toBe(2)
    // The first create to resolve activates; the second stays in the background.
    await expect(page).toHaveURL(url => url.searchParams.get('sid') === createdKeys[0])
    await expect(composer).toHaveValue('typed after two presses')
  })

  test('a file attached during the create keeps the whole draft together in the old session', async ({ page }) => {
    const originKey = await openSeededSession(page)
    const composer = page.locator('textarea[data-composer-input]')
    await delaySlotCreate(page)

    await composer.click()
    const created = waitForCreate(page)
    await page.keyboard.press('Alt+Shift+N')
    await page.keyboard.type('caption for the file')
    await page.locator('input[type="file"][multiple]').first().setInputFiles({ name: 'carry-note.txt', mimeType: 'text/plain', buffer: Buffer.from('hello') })
    await expect(page.getByText('carry-note.txt').first()).toBeVisible()
    const { key } = await (await created).json() as { key: string }
    await expect(page).toHaveURL(url => url.searchParams.get('sid') === key)
    // Moving the text without its file would split the draft; nothing moves.
    await expect(composer).toHaveValue('')

    await page.goto(`/chat?sid=${encodeURIComponent(originKey)}`, { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveURL(url => url.searchParams.get('sid') === originKey)
    await expect(composer).toBeVisible({ timeout: 10000 })
    await expect(composer).toHaveValue('caption for the file')
    await expect(page.getByText('carry-note.txt').first()).toBeVisible()
  })

  test('switching away and back while the create is pending still carries the typed text', async ({ page }) => {
    const other = await (await page.request.post('/api/chat/slots', { data: { agent: 'default' } })).json() as { key: string }
    const originKey = await openSeededSession(page)
    const composer = page.locator('textarea[data-composer-input]')
    const row = (key: string) => page.locator(`[data-slot-key="${key}"]`).first()
    await expect(row(other.key)).toBeVisible({ timeout: 10000 })
    await delaySlotCreate(page, 5000)

    await composer.click()
    const created = waitForCreate(page)
    await page.keyboard.press('Alt+Shift+N')
    await page.keyboard.type('typed then switched')
    await row(other.key).click()
    await expect(page).toHaveURL(url => url.searchParams.get('sid') === other.key)
    await row(originKey).click()
    await expect(page).toHaveURL(url => url.searchParams.get('sid') === originKey)
    const { key } = await (await created).json() as { key: string }
    await expect(page).toHaveURL(url => url.searchParams.get('sid') === key)
    await expect(composer).toHaveValue('typed then switched')
  })

  test('a file staged before the create keeps its caption with it in the old session', async ({ page }) => {
    const originKey = await openSeededSession(page)
    const composer = page.locator('textarea[data-composer-input]')
    await page.locator('input[type="file"][multiple]').first().setInputFiles({ name: 'staged-note.txt', mimeType: 'text/plain', buffer: Buffer.from('one') })
    await expect(page.getByText('staged-note.txt').first()).toBeVisible()
    await delaySlotCreate(page)

    await composer.click()
    const created = waitForCreate(page)
    await page.keyboard.press('Alt+Shift+N')
    await page.keyboard.type('caption for the staged file')
    const { key } = await (await created).json() as { key: string }
    await expect(page).toHaveURL(url => url.searchParams.get('sid') === key)
    await expect(composer).toHaveValue('')

    await page.goto(`/chat?sid=${encodeURIComponent(originKey)}`, { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveURL(url => url.searchParams.get('sid') === originKey)
    await expect(composer).toBeVisible({ timeout: 10000 })
    await expect(composer).toHaveValue('caption for the staged file')
    await expect(page.getByText('staged-note.txt').first()).toBeVisible()
  })

  test('an attachment swapped for another during the create keeps the draft together', async ({ page }) => {
    const originKey = await openSeededSession(page)
    const composer = page.locator('textarea[data-composer-input]')
    const fileInput = page.locator('input[type="file"][multiple]').first()
    await fileInput.setInputFiles({ name: 'first-note.txt', mimeType: 'text/plain', buffer: Buffer.from('one') })
    await expect(page.getByText('first-note.txt').first()).toBeVisible()
    await delaySlotCreate(page)

    await composer.click()
    const created = waitForCreate(page)
    await page.keyboard.press('Alt+Shift+N')
    await page.keyboard.type('caption')
    // Same count, different file: remove the first, attach a second.
    await page.getByRole('button', { name: 'Remove', exact: true }).first().click()
    await fileInput.setInputFiles({ name: 'second-note.txt', mimeType: 'text/plain', buffer: Buffer.from('two') })
    await expect(page.getByText('second-note.txt').first()).toBeVisible()
    const { key } = await (await created).json() as { key: string }
    await expect(page).toHaveURL(url => url.searchParams.get('sid') === key)
    await expect(composer).toHaveValue('')

    await page.goto(`/chat?sid=${encodeURIComponent(originKey)}`, { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveURL(url => url.searchParams.get('sid') === originKey)
    await expect(composer).toBeVisible({ timeout: 10000 })
    await expect(composer).toHaveValue('caption')
    await expect(page.getByText('second-note.txt').first()).toBeVisible()
  })

  test('an existing draft in the old session stays there; only the new text moves', async ({ page }) => {
    const originKey = await openSeededSession(page)
    const composer = page.locator('textarea[data-composer-input]')
    await composer.fill('old draft')
    await delaySlotCreate(page)

    await composer.click()
    await page.keyboard.press('End')
    const created = waitForCreate(page)
    await page.keyboard.press('Alt+Shift+N')
    await page.keyboard.type('fresh text')
    const { key } = await (await created).json() as { key: string }
    await expect(page).toHaveURL(url => url.searchParams.get('sid') === key)
    await expect(composer).toHaveValue('fresh text')

    await page.goto(`/chat?sid=${encodeURIComponent(originKey)}`, { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveURL(url => url.searchParams.get('sid') === originKey)
    await expect(composer).toBeVisible({ timeout: 10000 })
    await expect(composer).toHaveValue('old draft')
  })

  test('a large paste during the create moves with its content, not as a bare token', async ({ page }) => {
    const originKey = await openSeededSession(page)
    const composer = page.locator('textarea[data-composer-input]')
    await delaySlotCreate(page)

    await composer.click()
    const created = waitForCreate(page)
    await page.keyboard.press('Alt+Shift+N')
    const pasted = 'line one\nline two\nline three\nline four'
    await composer.evaluate((el, text) => {
      const data = new DataTransfer()
      data.setData('text/plain', text)
      el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }))
    }, pasted)
    await expect(composer).toHaveValue(/\[ Paste #\d+/)
    const { key } = await (await created).json() as { key: string }
    expect(key).not.toBe(originKey)
    await expect(page).toHaveURL(url => url.searchParams.get('sid') === key)

    await expect(composer).toHaveValue(/\[ Paste #\d+/)
    // The block came along: the new session's stored paste list holds the
    // content, so the send expands the token instead of sending it literally.
    // Drafts persist on a debounce, so poll the store rather than read it once.
    await expect.poll(async () => JSON.stringify(await page.evaluate(
      k => JSON.parse(localStorage.getItem('mc-chat-paste-drafts') || '{}')[k], key,
    ) ?? null)).toContain('line four')
  })

  test('?new=1 window: typing before the blank session exists', async ({ page }) => {
    await delaySlotCreate(page)
    const created = waitForCreate(page)
    await page.goto('/chat?new=1', { waitUntil: 'domcontentloaded' })
    const composer = page.locator('textarea[data-composer-input]')
    await composer.click({ timeout: 10000 })
    await page.keyboard.type('first words')
    const { key } = await (await created).json() as { key: string }
    await expect(page).toHaveURL(url => url.searchParams.get('sid') === key)
    await expect(composer).toHaveValue('first words')
  })
})
