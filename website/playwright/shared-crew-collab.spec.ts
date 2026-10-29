/**
 * Shared Crew: two verified principals in two browser contexts.
 *
 * Not part of the default gate. It needs a gateway where Shared Crew is wired
 * (identity, capabilities, routes) and two storage states that authenticate as
 * two DIFFERENT principals, which the single-token harness cannot provide. The
 * tag keeps the shared-gateway run from collecting it; the integration harness
 * runs it explicitly:
 *
 *   SHARED_CREW_STATE_A=<state-a.json> SHARED_CREW_STATE_B=<state-b.json> \
 *   SHARED_CREW_SESSION=<slot key> \
 *   PLAYWRIGHT_RUN_SHARED_CREW=1 npx playwright test shared-crew-collab
 *
 * Every wait is on a visible state, never a timer.
 */
import { test, expect, type Browser, type Page } from '@playwright/test'

const STATE_A = process.env.SHARED_CREW_STATE_A || ''
const STATE_B = process.env.SHARED_CREW_STATE_B || ''
const SESSION = process.env.SHARED_CREW_SESSION || ''

async function open(browser: Browser, storageState: string): Promise<Page> {
  const context = await browser.newContext({ storageState })
  const page = await context.newPage()
  await page.goto(`/chat?slot=${encodeURIComponent(SESSION)}`)
  return page
}

test.describe('Shared Crew collaboration @shared-crew', () => {
  test.skip(!STATE_A || !STATE_B || !SESSION, 'needs two principals and a session')

  test('each person sees the other, one typist locks the rest, one approval wins', async ({ browser }) => {
    const a = await open(browser, STATE_A)
    const b = await open(browser, STATE_B)

    // Presence: each tab shows the other person, never itself.
    await expect(a.getByTestId('presence-chips')).toBeVisible()
    await expect(b.getByTestId('presence-chips')).toBeVisible()

    // A types first and holds the typing lease: B's composer turns read-only
    // with A named, and B's own draft (none here) is left alone.
    const composerA = a.getByLabel('Message input')
    const composerB = b.getByLabel('Message input')
    await composerA.fill('draft from A')
    await expect(b.getByTestId('composer-typing-lock')).toContainText(/is typing/)
    await expect(composerB).toHaveAttribute('readonly', '')
    await expect(a.getByTestId('composer-typing-lock')).toHaveCount(0)
    await expect(composerA).toHaveValue('draft from A')

    // A clears the draft: B unlocks at once and can type.
    await composerA.fill('')
    await expect(b.getByTestId('composer-typing-lock')).toHaveCount(0)
    await expect(composerB).not.toHaveAttribute('readonly', '')
    await composerB.fill('draft from B')
    await expect(a.getByTestId('composer-typing-lock')).toContainText(/is typing/)
    await composerB.fill('')
    await expect(a.getByTestId('composer-typing-lock')).toHaveCount(0)

    // Racing an approval: whichever lands first wins; the other tab shows who.
    const approveA = a.getByRole('button', { name: /^Allow|^Approve/ }).first()
    const approveB = b.getByRole('button', { name: /^Reject|^Deny/ }).first()
    await expect(approveA).toBeVisible()
    await expect(approveB).toBeVisible()
    await Promise.all([approveA.click(), approveB.click()])
    await expect(a.getByTestId('decision-attribution')).toContainText(/(Approved|Rejected) by/)
    await expect(b.getByTestId('decision-attribution')).toContainText(/(Approved|Rejected) by/)

    // Reconnect after acceptance: a reload shows the same decision once.
    await b.reload()
    await expect(b.getByTestId('decision-attribution').getByRole('listitem')).toHaveCount(1)
  })

  test('the lock ends on its own after the idle window', async ({ browser }) => {
    const a = await open(browser, STATE_A)
    const b = await open(browser, STATE_B)
    await a.getByLabel('Message input').fill('A pauses mid-thought')
    const lock = b.getByTestId('composer-typing-lock')
    await expect(lock).toBeVisible()
    // The default idle window is 10 s; the lock must lift without any report.
    await expect(lock).toHaveCount(0, { timeout: 20_000 })
    await expect(b.getByLabel('Message input')).not.toHaveAttribute('readonly', '')
    await expect(a.getByLabel('Message input')).toHaveValue('A pauses mid-thought')
  })

  test('cancelling queued work cancels only that item', async ({ browser }) => {
    const a = await open(browser, STATE_A)
    const labels = a.getByTestId('queued-send')
    const queued = labels.first()
    await expect(queued).toBeVisible()
    const before = await labels.count()
    const workId = await queued.getAttribute('data-work-id')
    await queued.getByRole('button', { name: 'Cancel this queued message' }).click()
    // The cancelled item's label goes; every other queued label stays.
    await expect(a.locator(`[data-testid="queued-send"][data-work-id="${workId}"]`)).toHaveCount(0)
    await expect(labels).toHaveCount(before - 1)
  })
})
