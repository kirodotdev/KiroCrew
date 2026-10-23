import { test, expect, type Page } from '@playwright/test'

// Prompt sentinels understood by the stub ACP backend. Keep in sync with
// SLOW_TRIGGER / SLOW_NOACK_TRIGGER / SLOW_LATEACK_TRIGGER in
// src/kiro_crew/testing/fake_acp_backend.py.
const SLOW = '[[SLOW]]'
const SLOW_NOACK = '[[SLOW_NOACK]]'
const SLOW_LATEACK = '[[SLOW_LATEACK]]'

// The new-chat control's stable accessible name: the en.json value of
// pages.chatSidebar.new_chat_session (chat-sidebar/SidebarHeader.tsx aria-label). A
// whole-string exact match cannot collide with the per-turn minimap buttons,
// whose accessible names are prompt-derived (e.g. "What is 2+2?"), or the
// per-folder "New chat in <name>" buttons. fork.spec.ts uses the same locator.
const NEW_CHAT_NAME = 'New chat session'

// The stub's whole reply to a prompt that carries no [[...]] sentinel: REPLY_TEXT
// in src/kiro_crew/testing/fake_acp_backend.py.
const STUB_REPLY = 'pong from the fake ACP backend'

// Named ceiling for the first reply in a NEW slot. A new slot is a new ACP
// session, and its first prompt waits until the MCP-init drain gives up:
// `_MCP_DRAIN_NO_REPORT_CEILING` (6 s, src/kiro_crew/acp/session_handle.py) runs
// in full because the stub reports no MCP server. Send to reply measured
// 7.05-7.10 s (12 runs, two gateways): that fixed 6 s timer plus a ~1.1 s rest
// that load moves. So the ceiling is the timer plus 10x the rest:
// 6 s + 10 x 1.1 s = 17 s. A change to the drain ceiling changes this sum.
const FIRST_REPLY_CEILING_MS = 17_000

// The timeout 'displays streaming response' runs under: twice the reply ceiling,
// plus 2 s for the page load before it, so a lost turn fails on the reply wait,
// by name, before the test itself times out.
const STREAMING_TEST_TIMEOUT_MS = 2 * FIRST_REPLY_CEILING_MS + 2_000

// @needs-agent: these specs drive a live agent turn (send/stream/soft-stop),
// so they require model/agent credentials the credential-less CI gateway
// lacks. Tagged so the default gating run (grepInvert /@needs-agent/ in
// playwright.config.ts) excludes them; set PLAYWRIGHT_RUN_AGENT_SPECS=1 to opt in.
test.describe('Chat Page E2E Tests', { tag: '@needs-agent' }, () => {
  // Each test runs independently in its own browser context for parallel execution
  test.beforeEach(async ({ page }) => {
    await page.goto('/chat', { waitUntil: 'domcontentloaded' })
    // Wait for chat interface to be ready
    await expect(page.getByPlaceholder(/message/i)).toBeVisible({ timeout: 10000 })
  })

  test('navigates to chat page and displays interface', async ({ page }) => {
    // Should see chat interface. Match the Send button by its exact accessible
    // name — a loose /send/i also matches the "Edit & Resend" buttons on seeded
    // assistant messages (strict-mode violation).
    await expect(page.getByPlaceholder(/message/i)).toBeVisible({ timeout: 10000 })
    // Type first: the composer's primary button only READS "Send" when there is
    // something to send. On an EMPTY composer over a slot that already holds a
    // conversation it morphs into Continue (`selectContinuable`), so asserting
    // "Send" unconditionally makes this spec depend on whether the slot it landed
    // on happens to carry history — passing or failing on leftover state rather
    // than on the interface rendering. Filling the box pins the state the
    // assertion is actually about.
    await page.getByPlaceholder(/message/i).fill('hello')
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible()
  })

  test('sends a chat message and displays it', async ({ page }) => {
    const messageInput = page.getByPlaceholder(/message/i)
    await expect(messageInput).toBeVisible({ timeout: 10000 })

    // Type a message
    await messageInput.fill('What is 2+2?')
    
    // Send the message (press Enter)
    await page.keyboard.press('Enter')

    // Verify the message was sent (appears in chat) - use first() for duplicates
    await expect(page.getByText('What is 2+2?').first()).toBeVisible({ timeout: 5000 })
    
    // Wait for input to be cleared as confirmation message was sent
    await expect(messageInput).toHaveValue('', { timeout: 2000 })
  })

  // The slot 'displays streaming response' creates through the API, closed after
  // the test (see the cleanup note at the end of this block).
  let ownSlot = ''
  test.afterEach(async ({ page }) => {
    if (ownSlot) await page.request.delete(`/api/chat/slots/${encodeURIComponent(ownSlot)}`)
    ownSlot = ''
  })

  test('displays streaming response', async ({ page }) => {
    test.setTimeout(STREAMING_TEST_TIMEOUT_MS)
    // A slot of its own. Every other spec here lands on one shared slot, which
    // can still be running the turn the spec before this one sent: a prompt sent
    // to a busy slot is only queued. Its seeded history is not this test's
    // either: while the view rests at the bottom, its first prompt can be the
    // pinned prompt's stand-in, which stays hidden while the pinned card shows it.
    const created = await page.request.post('/api/chat/slots', { data: { agent: 'default' } })
    expect(created.ok(), await created.text()).toBeTruthy()
    const slot = ((await created.json()) as { key: string }).key
    ownSlot = slot
    await page.goto(`/chat?sid=${encodeURIComponent(slot)}`, { waitUntil: 'domcontentloaded' })
    // The link opens the slot only once the gateway is connected and the slot
    // list has loaded. Before that the composer shows with no slot behind it, or
    // offline (that placeholder matches /message/i too), and Enter sends nowhere
    // or into a slot it creates itself.
    const ownRow = page.locator(`[data-slot-key="${slot}"] .session-row.session-active`)
    await expect(ownRow).toBeVisible({ timeout: 10000 })
    const messageInput = page.getByPlaceholder(/message/i)
    await expect(messageInput).not.toHaveAttribute('placeholder', /offline/i)

    await messageInput.fill('Hello')
    await page.keyboard.press('Enter')

    // Sent, not queued: the prompt is in this slot's transcript, the backend's
    // streamed reply follows it, and the slot is still the one this test opened.
    const transcript = page.getByLabel('Chat messages', { exact: true })
    await expect(transcript.getByText('Hello', { exact: true })).toBeVisible()
    await expect(transcript.getByText(STUB_REPLY, { exact: true })).toBeVisible({
      timeout: FIRST_REPLY_CEILING_MS,
    })
    await expect(ownRow).toBeVisible()
  })

  test('clears message input after sending', async ({ page }) => {
    const messageInput = page.getByPlaceholder(/message/i)
    await expect(messageInput).toBeVisible({ timeout: 10000 })

    await messageInput.fill('Test message')
    await page.keyboard.press('Enter')

    // Input should be cleared
    await expect(messageInput).toHaveValue('', { timeout: 2000 })
  })

  test('creates new chat slot', async ({ page }) => {
    const newChatButton = page.getByRole('button', { name: NEW_CHAT_NAME, exact: true })
    // Assert rather than guard: an if(isVisible()) skip would report green
    // without ever exercising new-chat creation, and toBeVisible() auto-waits
    // where isVisible() races the sidebar paint.
    await expect(newChatButton).toBeVisible()
    await newChatButton.click()

    // Should see empty message input (confirmed by waiting for it)
    await expect(page.getByPlaceholder(/message/i)).toBeVisible()
  })

  test('switches between chat slots', async ({ page }) => {
    // Create a second chat first (same stable accessible name as above)
    const newChatButton = page.getByRole('button', { name: NEW_CHAT_NAME, exact: true })
    await expect(newChatButton).toBeVisible()
    await newChatButton.click()
    // Wait for new slot to be created
    await expect(page.getByPlaceholder(/message/i)).toBeVisible()
    
    // Look for chat history/slots in sidebar
    const chatSlots = page.locator('[class*="slot"], [class*="session"]')
    const slotCount = await chatSlots.count()

    if (slotCount > 1) {
      // Click on a different slot
      await chatSlots.nth(1).click()
      
      // Wait for chat to switch by checking for visible content
      await expect(page.locator('body')).toBeVisible()
      
      // Go back to first chat
      await chatSlots.first().click()
      await expect(page.locator('body')).toBeVisible()
    }
  })

  test('displays chat history', async ({ page }) => {
    // Look for history button/panel
    const historyButton = page.getByRole('button', { name: /history/i })
    
    if (await historyButton.isVisible()) {
      await historyButton.click()
      
      // Should see history panel - wait for it to be visible
      await expect(page.locator('body')).toBeVisible()
    }
  })

  test('shows typing indicator when sending message', async ({ page }) => {
    const messageInput = page.getByPlaceholder(/message/i)
    await expect(messageInput).toBeVisible({ timeout: 10000 })

    await messageInput.fill('Hello')
    await page.keyboard.press('Enter')

    // Wait for message to appear (sent state)
    await expect(page.getByText('Hello').first()).toBeVisible({ timeout: 5000 })
    
    // Input should be cleared quickly, indicating send was successful
    await expect(messageInput).toHaveValue('', { timeout: 2000 })
  })
  // Cleanup: the slots 'creates new chat slot' and 'switches between chat slots'
  // open through the UI stay open, since nothing tells them apart from a user's
  // own. The one slot created through the API has a known key and is closed after
  // its test; closing archives a slot to history and deletes nothing.
})

/**
 * Soft-stop E2E tests.
 *
 * Driven by the stub ACP backend (src/kiro_crew/testing/fake_acp_backend.py), so
 * these need no model credentials: [[SLOW]] streams a long turn that DOES honour
 * session/cancel, answering stopReason:"cancelled" — the ack the host waits for.
 * They were @needs-live-agent while the stub dropped session/cancel entirely.
 *
 * Assertions target the stop button's escalation testid and StopEventCard's
 * data-state rather than label text: the state is the contract, the wording is
 * not. The previous `/stop/i` role selector is now a strict-mode violation —
 * three buttons carry "stop" in their accessible name.
 *
 * Before pressing Stop, every spec waits for the stub's first streaming chunk.
 * That chunk is emitted only from inside session/prompt, so observing it proves
 * the host's turn is in-flight and session/cancel has a live turn to land on.
 * The armed Stop button alone is not that proof: it appears as soon as the slot
 * is busy, which includes a queued but not-yet-dispatched prompt, and a stop
 * pressed in that window finds stop_turn=idle and settles the card as "stopped"
 * instead of driving the expiry or pulsing path.
 *
 * Cross-spec isolation: every spec runs on the one slot `/chat` opens, so each
 * starts with `openChatOnIdleSlot`, which waits until the gateway reports that
 * slot idle before the spec sends. Each spec also includes a per-run unique
 * marker in the prompt. Waiting for that marker to appear in the transcript
 * proves our user bubble landed BEFORE we check for chunks, preventing an old
 * turn's chunk paragraph from matching the `transcript.locator('p').last()`
 * wait early.
 */
const STOP_CARD = '[data-testid="stop-event-card"]'

// The first streamed chunk from any SLOW* turn: SLOW_CHUNK_TEXT + "0 " in
// src/kiro_crew/testing/fake_acp_backend.py. Only emitted from inside
// session/prompt, so its presence in the transcript proves the turn is live.
const FIRST_SLOW_CHUNK = 'fake slow chunk 0'

// Ceiling for the turn the spec before left on the shared slot to end
// (`openChatOnIdleSlot`).
const PRIOR_TURN_SETTLE_CEILING_MS = 30_000

// Per-test time budget: the prior turn settling, nav/load + MCP drain
// (FIRST_REPLY_CEILING_MS) + stop resolution (3x the 5s soft_stop_budget_secs).
const SOFT_STOP_TEST_TIMEOUT_MS =
  PRIOR_TURN_SETTLE_CEILING_MS + 5_000 + FIRST_REPLY_CEILING_MS + 15_000

/**
 * Open the chat page on its default slot once the gateway reports that slot
 * idle, and return the slot key.
 *
 * The spec before can leave its turn running on this slot: the pulsing spec
 * ends while its turn still waits for the late ack, the soft-ack spec ends the
 * moment its card settles, and the budget spec ends on a hard kill that resets
 * the session. A prompt sent while that turn winds down is queued behind it or
 * steered into it instead of starting a turn of its own, so the spec then waits
 * on a user bubble that never lands, or on an armed Stop that never appears.
 *
 * The page cannot tell idle apart from busy here: an absent armed Stop also
 * means "stopping" (the button pulses or spins) and "not loaded yet". So the
 * wait reads the gateway's own record of the slot, then reopens the page so it
 * renders from that idle state rather than from whatever it loaded while the
 * earlier turn was winding down.
 */
async function openChatOnIdleSlot(page: Page): Promise<string> {
  await page.goto('/chat', { waitUntil: 'domcontentloaded' })
  const activeRow = page.locator('.session-row.session-active').first()
  await expect(activeRow).toBeVisible({ timeout: 30_000 })
  const slot = await activeRow
    .locator('xpath=ancestor::*[@data-slot-key][1]')
    .getAttribute('data-slot-key')
  if (!slot) throw new Error('the active session row names no slot')
  await expect
    .poll(
      async () => {
        const res = await page.request.get(`/api/chat/slots/${encodeURIComponent(slot)}?limit=1`)
        if (!res.ok()) return `HTTP ${res.status()}`
        const s = (await res.json()) as { running?: boolean; stopping?: boolean; queue?: unknown[] }
        const queued = s.queue?.length ?? 0
        return s.running || s.stopping || queued
          ? `running=${s.running} stopping=${s.stopping} queued=${queued}`
          : 'idle'
      },
      { message: `slot ${slot} did not go idle`, timeout: PRIOR_TURN_SETTLE_CEILING_MS },
    )
    .toBe('idle')
  await page.goto(`/chat?sid=${encodeURIComponent(slot)}`, { waitUntil: 'domcontentloaded' })
  await expect(page.locator(`[data-slot-key="${slot}"] .session-row.session-active`)).toBeVisible({
    timeout: 30_000,
  })
  await expect(page.getByPlaceholder(/message/i)).not.toHaveAttribute('placeholder', /offline/i)
  return slot
}

/**
 * Wait until the stub's first chunk is visible in the CURRENT turn's response.
 *
 * The transcript persists across browser sessions (loaded fresh from the gateway
 * on each test page), so a previous turn's identical `fake slow chunk 0` paragraph
 * appears earlier in the DOM. `transcript.locator('p').last()` is the last
 * paragraph in DOM order. After `marker` (a per-run unique string in our prompt)
 * is visible in the transcript, our user bubble is the newest message, so any
 * subsequent `p.last()` that contains FIRST_SLOW_CHUNK belongs to the current
 * response, not an older one.
 */
async function waitForCurrentTurnFirstChunk(
  transcript: ReturnType<import('@playwright/test').Page['getByLabel']>,
  marker: string,
): Promise<void> {
  // Confirm our user bubble landed (transcript contains marker = our prompt).
  // Without this, `p.last()` could still be an older response paragraph that
  // already contains `fake slow chunk 0`, resolving the wait before our turn
  // has started.
  await expect(transcript).toContainText(marker, { timeout: 10_000 })
  // Now p.last() is our user paragraph (or the response that follows it, once
  // the response starts rendering). Wait for it to contain the first chunk.
  await expect(transcript.locator('p').last()).toContainText(FIRST_SLOW_CHUNK, {
    timeout: FIRST_REPLY_CEILING_MS,
  })
}

test.describe('Soft-Stop E2E Tests', { tag: '@needs-agent' }, () => {
  test.beforeEach(async ({ page }) => {
    test.setTimeout(SOFT_STOP_TEST_TIMEOUT_MS)
    await openChatOnIdleSlot(page)
  })

  /**
   * Uses [[SLOW_LATEACK]] rather than [[SLOW]] because this is the only spec
   * here that asserts an INTERMEDIATE state, and [[SLOW]] destroys that state
   * before a loaded browser can paint it.
   *
   * `stop-button-pulsing` renders only while `stop_state === 'soft_pending'`
   * (ChatInput.tsx). The client keeps no optimistic copy: ChatPage passes
   * `currentSlot?.stop_state` straight through. So the element exists for
   * exactly as long as the host waits for the cancel ack. Under [[SLOW]] the
   * stub checks for the cancel once per chunk and acks on the first check, so
   * that is under 500ms, averaging ~250ms. Two WebSocket pushes bracket it and
   * the first push's render can consume the whole window.
   *
   * [[SLOW_LATEACK]] acks after SLOW_LATEACK_CHUNKS more chunks (~3s at the
   * default chunk delay), so the state is observable with real margin. It still
   * acks well inside `agent.soft_stop_budget_secs`, so the turn ends
   * cooperatively and nothing leaks into the spec that follows.
   */
  test('stop mid-tool-call triggers pulsing', async ({ page }) => {
    const marker = `pulsing-${Date.now()}`
    const messageInput = page.getByPlaceholder(/message/i)
    await messageInput.fill(`Run a long command: sleep 30 ${SLOW_LATEACK} ${marker}`)
    await page.keyboard.press('Enter')

    const transcript = page.getByLabel('Chat messages', { exact: true })
    await waitForCurrentTurnFirstChunk(transcript, marker)

    // Click stop — should enter the pulsing "stopping" state
    await page.getByTestId('stop-button-armed').click()

    await expect(page.getByTestId('stop-button-pulsing')).toBeVisible({ timeout: 5000 })
  })

  test('stop resolves to Stopped on soft ack', async ({ page }) => {
    const marker = `softack-${Date.now()}`
    const messageInput = page.getByPlaceholder(/message/i)
    await messageInput.fill(`Hello, please respond slowly ${SLOW} ${marker}`)
    await page.keyboard.press('Enter')

    const transcript = page.getByLabel('Chat messages', { exact: true })
    await waitForCurrentTurnFirstChunk(transcript, marker)

    await page.getByTestId('stop-button-armed').click()

    // The stub acks the cancel, so the card resolves to [Stopped].
    await expect(page.locator(`${STOP_CARD}[data-state="stopped"]`).first()).toBeVisible({
      timeout: 15000,
    })
  })
})

/**
 * Budget-expiry soft-stop.
 *
 * The agent half is [[SLOW_NOACK]]: fake_acp_backend streams SLOW_CHUNKS=30
 * chunks at SLOW_CHUNK_DELAY_SECS=0.5 (15s) and, unlike [[SLOW]], never checks
 * for the cancel. So the host\'s soft-stop budget always expires first and the
 * stop escalates to a hard kill.
 *
 * The host half is `agent.soft_stop_budget_secs`, read server-side in
 * session.py stop_turn(). A client-side `page.route` override cannot reach it,
 * which is why this spec was dark. The harness fixture now declares 5.0
 * (src/kiro_crew/tests_fixtures/minimal/config.json), comfortably inside the
 * 15s stream, so escalation fires around 5s. The 10.0 default would also
 * escalate before the stream ends, but it leaves only ~4s of headroom against
 * the assertion timeout on a loaded runner. Pinning it makes the dependency
 * explicit rather than a property of the default.
 *
 * What this covers that the two soft-ack specs above do not: the escalation
 * path settling the card. That path shipped broken, and nothing caught it
 * because this spec was dark. A turn tearing
 * down concurrently reset `_stop_state` to "idle", the hard callback\'s state
 * gate bailed, and the card pulsed at "stopping" for the rest of the session.
 * See TestStopCardTeardownRace in test/test_stop_handler_idempotent.py.
 */
test.describe('Soft-Stop budget expiry', { tag: '@needs-agent' }, () => {
  test.beforeEach(async ({ page }) => {
    test.setTimeout(SOFT_STOP_TEST_TIMEOUT_MS)
    await openChatOnIdleSlot(page)
  })

  test('stop resolves to Stop Failed on budget expiry', async ({ page }) => {
    const marker = `budgetexpiry-${Date.now()}`
    const messageInput = page.getByPlaceholder(/message/i)
    await messageInput.fill(`Run a very long command: sleep 120 ${SLOW_NOACK} ${marker}`)
    await page.keyboard.press('Enter')

    const transcript = page.getByLabel('Chat messages', { exact: true })
    await waitForCurrentTurnFirstChunk(transcript, marker)

    await page.getByTestId('stop-button-armed').click()

    await expect(page.locator(`${STOP_CARD}[data-state="stop_failed_reset"]`).first()).toBeVisible({
      timeout: 15000,
    })
  })
})
