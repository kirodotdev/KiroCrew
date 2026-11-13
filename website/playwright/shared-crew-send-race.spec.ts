/**
 * Shared Crew first submit wins: two verified principals send into one idle
 * session at the same revision, the backstop behind the typing lease.
 *
 * Not part of the default gate, for the same reason as shared-crew-collab: it
 * needs a gateway with Shared Crew wired and two storage states for two
 * DIFFERENT principals. The integration harness runs it explicitly:
 *
 *   SHARED_CREW_STATE_A=<state-a.json> SHARED_CREW_STATE_B=<state-b.json> \
 *   SHARED_CREW_SESSION=<idle slot key> \
 *   PLAYWRIGHT_RUN_SHARED_CREW=1 npx playwright test shared-crew-send-race
 *
 * Every wait is on a visible state or an intercepted request, never a timer.
 */
import { test, expect, type Browser, type Page, type Route } from '@playwright/test'

const STATE_A = process.env.SHARED_CREW_STATE_A || ''
const STATE_B = process.env.SHARED_CREW_STATE_B || ''
const SESSION = process.env.SHARED_CREW_SESSION || ''
const SEND_URL = /\/api\/chat\?ws=1$/

async function open(browser: Browser, storageState: string): Promise<Page> {
  const context = await browser.newContext({ storageState, locale: 'en-US' })
  const page = await context.newPage()
  // This scenario exercises the send backstop, not typing-lease acquisition.
  // All server frames and non-typing client frames still use the real socket.
  await page.routeWebSocket(/\/api\/ws(?:\?|$)/, socket => {
    const server = socket.connectToServer()
    socket.onMessage(message => {
      let frame: Record<string, unknown>
      try { frame = JSON.parse(message.toString()) as Record<string, unknown> }
      catch { server.send(message); return }
      if (frame.type === 'shared_crew.activity' && frame.session_key === SESSION && frame.activity === 'typing') return
      server.send(message)
    })
  })
  const seeded = page.waitForResponse(response => {
    const url = new URL(response.url())
    return url.pathname === '/api/shared-crew/send-state' && url.searchParams.get('session_key') === SESSION
  })
  await page.goto(`/chat?slot=${encodeURIComponent(SESSION)}`)
  const response = await seeded
  expect(response.ok()).toBe(true)
  expect((await response.json()).revision).toEqual(expect.any(Number))
  return page
}

test.describe('Shared Crew send race @shared-crew', () => {
  test.skip(!STATE_A || !STATE_B || !SESSION, 'needs two principals and an idle session')

  test('one send wins, the other keeps its draft and can Send now', async ({ browser }) => {
    const a = await open(browser, STATE_A)
    const b = await open(browser, STATE_B)
    try {
      const composerA = a.getByLabel('Message input')
      const composerB = b.getByLabel('Message input')
      await expect(composerA).toBeEditable()
      await expect(composerB).toBeEditable()
      const held = new Map<Page, Route>()
      for (const page of [a, b]) {
        await page.route(SEND_URL, route => { held.set(page, route) }, { times: 1 })
      }
      await Promise.all([composerA.fill('first from A'), composerB.fill('first from B')])
      await Promise.all([composerA.press('Enter'), composerB.press('Enter')])

      // Both real POSTs must be parked before either reaches the gateway.
      // Deliver A first, then B's already-serialized stale revision: neither
      // socket scheduling nor the duration of A's turn can change the refusal.
      await expect.poll(() => held.size).toBe(2)
      const requestA = held.get(a)!
      const requestB = held.get(b)!
      const bodyA = requestA.request().postDataJSON()
      const bodyB = requestB.request().postDataJSON()
      expect(bodyA).toMatchObject({ slot: SESSION, message: 'first from A', base_revision: expect.any(Number) })
      expect(bodyB).toMatchObject({ slot: SESSION, message: 'first from B', base_revision: bodyA.base_revision })
      expect(bodyA.steer).toBeUndefined()
      expect(bodyB.steer).toBeUndefined()
      const accepted = a.waitForResponse(response => response.request() === requestA.request())
      await requestA.continue()
      const acceptedResponse = await accepted
      expect(acceptedResponse.ok()).toBe(true)
      expect(await acceptedResponse.json()).toMatchObject({ ok: true })
      const refused = b.waitForResponse(response => response.request() === requestB.request())
      await requestB.continue()
      const refusedResponse = await refused
      expect(refusedResponse.status()).toBe(409)
      expect(await refusedResponse.json()).toMatchObject({ code: 'send_race', session_key: SESSION })

      await expect(a.getByTestId('send-race-notice')).toHaveCount(0)
      await expect(b.getByTestId('send-race-notice')).toContainText(/sent first/)
      await expect(composerB).toHaveValue('first from B')

      // Send now is a new send: it is accepted (running or queued), never a steer.
      const retried = b.waitForResponse(response => SEND_URL.test(response.url()) && response.request().method() === 'POST')
      await b.getByTestId('send-race-send-now').click()
      const retryResponse = await retried
      expect(retryResponse.ok()).toBe(true)
      expect(retryResponse.request().postDataJSON()).toMatchObject({ send_now: true })
      expect(retryResponse.request().postDataJSON().steer).toBeUndefined()
      await expect(b.getByTestId('send-race-notice')).toHaveCount(0)
      await expect(composerB).toHaveValue('')

      // Reconnect reconciles from server truth: one row per accepted send, and
      // nothing is resubmitted by the reload.
      await b.reload()
      await expect(b.getByText('first from A', { exact: true })).toHaveCount(1)
      await expect(b.getByText('first from B', { exact: true })).toHaveCount(1)
    } finally {
      await Promise.all([a.context().close(), b.context().close()])
    }
  })
})
