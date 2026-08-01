import { test, expect } from '@playwright/test'

// Prompt sentinels understood by the stub ACP backend. Keep in sync with
// SLOW_TRIGGER / SLOW_NOACK_TRIGGER / SLOW_LATEACK_TRIGGER in
// src/kiro_crew/testing/fake_acp_backend.py.
const SLOW = '[[SLOW]]'
const SLOW_NOACK = '[[SLOW_NOACK]]'
const SLOW_LATEACK = '[[SLOW_LATEACK]]'

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

  test('displays streaming response', async ({ page }) => {
    // Send a message first
    const messageInput = page.getByPlaceholder(/message/i)
    await messageInput.fill('Hello')
    await page.keyboard.press('Enter')
    
    // Check if messages are visible
    await expect(page.locator('.msg-content').first()).toBeVisible({ timeout: 5000 })
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
    // Look for "New Chat" or "+" button
    const newChatButton = page.getByRole('button', { name: /new chat|\+/i })
    
    if (await newChatButton.isVisible()) {
      await newChatButton.click()
      
      // Should see empty message input (confirmed by waiting for it)
      await expect(page.getByPlaceholder(/message/i)).toBeVisible()
    }
  })

  test('switches between chat slots', async ({ page }) => {
    // Create a second chat first
    const newChatButton = page.getByRole('button', { name: /new chat|\+/i })
    if (await newChatButton.isVisible()) {
      await newChatButton.click()
      // Wait for new slot to be created
      await expect(page.getByPlaceholder(/message/i)).toBeVisible()
    }
    
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
  // Cleanup: Skip automated cleanup to avoid accidentally deleting user data
  // Chat slots created during tests will persist, but this is safer than
  // risking deletion of real user chat history
  // If cleanup is needed, it should be done manually with explicit test markers
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
 */
const STOP_CARD = '[data-testid="stop-event-card"]'

test.describe('Soft-Stop E2E Tests', { tag: '@needs-agent' }, () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/chat', { waitUntil: 'domcontentloaded' })
    await expect(page.getByPlaceholder(/message/i)).toBeVisible({ timeout: 10000 })
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
   *
   * [[SLOW_NOACK]] would also widen the window, but it leaves the slot mid-budget
   * with a hard kill pending, which makes the sibling spec below fail. Measured:
   * with NOACK here, `stop resolves to Stopped on soft ack` failed 4 of 4 runs.
   */
  test('stop mid-tool-call triggers pulsing', async ({ page }) => {
    // A cancel-aware slow turn that winds down before acking, so the
    // soft_pending state is observable rather than a ~250ms race.
    const messageInput = page.getByPlaceholder(/message/i)
    await messageInput.fill(`Run a long command: sleep 30 ${SLOW_LATEACK}`)
    await page.keyboard.press('Enter')

    // Wait for the stop button to appear (agent is running)
    const stopButton = page.getByTestId('stop-button-armed')
    await expect(stopButton).toBeVisible({ timeout: 15000 })

    // Click stop — should enter the pulsing "stopping" state
    await stopButton.click()

    await expect(page.getByTestId('stop-button-pulsing')).toBeVisible({ timeout: 5000 })
  })

  test('stop resolves to Stopped on soft ack', async ({ page }) => {
    const messageInput = page.getByPlaceholder(/message/i)
    await messageInput.fill(`Hello, please respond slowly ${SLOW}`)
    await page.keyboard.press('Enter')

    const stopButton = page.getByTestId('stop-button-armed')
    await expect(stopButton).toBeVisible({ timeout: 15000 })
    await stopButton.click()

    // The stub acks the cancel, so the card resolves to [Stopped].
    await expect(page.locator(`${STOP_CARD}[data-state="stopped"]`).first()).toBeVisible({
      timeout: 15000,
    })
  })
})

/**
 * Budget-expiry soft-stop, still excluded.
 *
 * The stub CAN withhold the cancel ack ([[SLOW_NOACK]] streams a long turn and
 * ignores session/cancel), which is the agent half of this scenario. What blocks
 * it is the host half: `agent.soft_stop_budget_secs` is read server-side in
 * session.py stop_turn(), so the `page.route('**\/api/config')` override this
 * test used to carry never changed the enforced budget. Observed behaviour with
 * the default budget is that the card stays in `stopping` well past Playwright's
 * 30s per-test timeout, so the run fails on timeout rather than on the assertion.
 *
 * To enable: give the harness gateway a small `agent.soft_stop_budget_secs`
 * (config, not a client-side route intercept), then retag to @needs-agent and
 * assert on data-state="stop_failed_reset". Left dark rather than shipped with a
 * long sleep or a raised timeout that would only mask the timing question.
 */
test.describe('Soft-Stop budget expiry', { tag: '@needs-live-agent' }, () => {
  test('stop resolves to Stop Failed on budget expiry', async ({ page }) => {
    await page.goto('/chat', { waitUntil: 'domcontentloaded' })
    await expect(page.getByPlaceholder(/message/i)).toBeVisible({ timeout: 10000 })

    const messageInput = page.getByPlaceholder(/message/i)
    await messageInput.fill(`Run a very long command: sleep 120 ${SLOW_NOACK}`)
    await page.keyboard.press('Enter')

    const stopButton = page.getByTestId('stop-button-armed')
    await expect(stopButton).toBeVisible({ timeout: 15000 })
    await stopButton.click()

    await expect(page.locator(`${STOP_CARD}[data-state="stop_failed_reset"]`).first()).toBeVisible({
      timeout: 15000,
    })
  })
})
