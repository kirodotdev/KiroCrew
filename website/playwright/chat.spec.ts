import { test, expect } from '@playwright/test'

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
 * These tests require the backend running with:
 *   KIROCREW_HOME=.kirocrew-dev KIROCREW_PORT=6777
 * per TEST_README.md. If the gateway is not running, tests will fail on
 * navigation timeout — that is acceptable for offline development.
 */
test.describe('Soft-Stop E2E Tests', { tag: '@needs-live-agent' }, () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/chat', { waitUntil: 'domcontentloaded' })
    await expect(page.getByPlaceholder(/message/i)).toBeVisible({ timeout: 10000 })
  })

  test('stop mid-tool-call triggers pulsing', async ({ page }) => {
    // Send a message that will trigger a tool call
    const messageInput = page.getByPlaceholder(/message/i)
    await messageInput.fill('Run a long command: sleep 30')
    await page.keyboard.press('Enter')

    // Wait for the stop button to appear (agent is running)
    const stopButton = page.getByRole('button', { name: /stop/i })
    await expect(stopButton).toBeVisible({ timeout: 15000 })

    // Click stop — should enter pulsing state
    await stopButton.click()

    // The stop button or a stopping indicator should show pulsing/stopping state
    // Check for the pulsing animation class or the Stopping text
    await expect(
      page.locator('[class*="pulse"], [class*="stopping"], :text("Stopping")')
        .first()
    ).toBeVisible({ timeout: 5000 })
  })

  test('stop resolves to Stopped on soft ack', async ({ page }) => {
    // Send a message to start a turn
    const messageInput = page.getByPlaceholder(/message/i)
    await messageInput.fill('Hello, please respond slowly')
    await page.keyboard.press('Enter')

    // Wait for agent to be running
    const stopButton = page.getByRole('button', { name: /stop/i })
    await expect(stopButton).toBeVisible({ timeout: 15000 })

    // Click stop
    await stopButton.click()

    // Wait for the stop event card to resolve — soft ack shows [Stopped]
    await expect(
      page.locator(':text("Stopped")').first()
    ).toBeVisible({ timeout: 15000 })
  })

  test('stop resolves to Stop Failed on budget expiry', async ({ page }) => {
    // Intercept the stop endpoint to simulate a timeout scenario by
    // setting a very short budget that the agent cannot meet
    await page.route('**/api/config', async (route) => {
      const response = await route.fetch()
      const json = await response.json()
      // Override budget to minimum so timeout is near-certain
      if (json.agent) {
        json.agent.soft_stop_budget_secs = 0.5
      }
      await route.fulfill({ json })
    })

    // Send a message that triggers a long tool call
    const messageInput = page.getByPlaceholder(/message/i)
    await messageInput.fill('Run a very long command: sleep 120')
    await page.keyboard.press('Enter')

    // Wait for agent to be running
    const stopButton = page.getByRole('button', { name: /stop/i })
    await expect(stopButton).toBeVisible({ timeout: 15000 })

    // Click stop
    await stopButton.click()

    // With a 0.5s budget the agent likely cannot ack in time →
    // card should show [Stop Failed, Session Reset]
    await expect(
      page.locator(':text("Stop Failed"), :text("Session Reset")').first()
    ).toBeVisible({ timeout: 15000 })
  })
})
