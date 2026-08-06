import { test, expect } from '@playwright/test'
import { pickFromDropdown } from './helpers/dropdown'

test.describe('Hooks Page E2E Tests', () => {
  test.beforeEach(async ({ page }) => {
    // Navigate directly to hooks page
    await page.goto('/hooks', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(500)
  })

  // Clean up test-created hooks after all tests
  test.afterAll(async ({ browser }) => {
    const page = await browser.newPage()
    await page.goto('/hooks', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(1000)

    // Delete "Playwright_Test_Hook" if it exists
    const testHooks = page.locator('div').filter({ hasText: /^Playwright_Test_Hook$/ })
    const testHookCount = await testHooks.count()
    
    for (let i = 0; i < testHookCount; i++) {
      // Find the delete button for the first matching hook
      const hookRow = page.locator('div').filter({ has: page.getByText('Playwright_Test_Hook') }).first()
      const deleteButton = hookRow.getByRole('button', { name: /delete/i }).first()
      
      if (await deleteButton.isVisible()) {
        page.on('dialog', dialog => dialog.accept())
        await deleteButton.click()
        await page.waitForTimeout(500)
      }
    }

    // Delete "Playwright_Updated_Hook" if it exists (from edit test)
    const updatedHooks = page.locator('div').filter({ hasText: /^Playwright_Updated_Hook$/ })
    const updatedHookCount = await updatedHooks.count()
    
    for (let i = 0; i < updatedHookCount; i++) {
      const hookRow = page.locator('div').filter({ has: page.getByText('Playwright_Updated_Hook') }).first()
      const deleteButton = hookRow.getByRole('button', { name: /delete/i }).first()
      
      if (await deleteButton.isVisible()) {
        page.on('dialog', dialog => dialog.accept())
        await deleteButton.click()
        await page.waitForTimeout(500)
      }
    }

    await page.close()
  })

  test('navigates to Hooks page and displays interface', async ({ page }) => {
    // Should see hooks page
    await expect(
      page.getByRole('button', { name: /\+ new hook/i })
    ).toBeVisible({ timeout: 10000 })

    // Should see "+ New Hook" button
    await expect(page.getByRole('button', { name: /\+ new hook/i })).toBeVisible()
  })

  test('displays existing hooks', async ({ page }) => {
    await expect(page.getByRole('button', { name: /\+ new hook/i })).toBeVisible({ timeout: 10000 })

    // Wait for hooks to load
    await page.waitForTimeout(1000)

    // Should see hooks container
    await expect(page.locator('body')).toContainText(/hooks/i)
  })

  test('creates a new hook', async ({ page }) => {
    await expect(page.getByRole('button', { name: /\+ new hook/i })).toBeVisible({ timeout: 10000 })

    // Click "+ New Hook" button
    await page.getByRole('button', { name: /\+ new hook/i }).click()

    // Should show create form
    await expect(page.getByPlaceholder(/hook name/i)).toBeVisible({ timeout: 3000 })

    // Fill in hook details
    await page.getByPlaceholder(/hook name/i).fill('Playwright_Test_Hook')
    await page.getByPlaceholder(/echo 'hook fired'/i).fill('echo "E2E test"')

    // Click save
    await page.getByRole('button', { name: /^save$/i }).click()

    // Form should close
    await expect(page.getByPlaceholder(/hook name/i)).not.toBeVisible({ timeout: 5000 })

    // Hook should appear in list - use first() in case it was created multiple times
    await expect(page.getByText('Playwright_Test_Hook').first()).toBeVisible({ timeout: 3000 })
  })

  test('cancels hook creation', async ({ page }) => {
    await page.getByRole('button', { name: /\+ new hook/i }).click()

    await expect(page.getByPlaceholder(/hook name/i)).toBeVisible({ timeout: 3000 })

    // Click cancel
    await page.getByRole('button', { name: /cancel/i }).click()

    // Form should close
    await expect(page.getByPlaceholder(/hook name/i)).not.toBeVisible({ timeout: 3000 })
  })

  test('edits an existing hook', async ({ page }) => {
    await expect(page.getByRole('button', { name: /\+ new hook/i })).toBeVisible({ timeout: 10000 })

    // Self-contained: create a hook with a known-valid event, then edit THAT
    // row. Using getByRole('button',{name:/edit/i}).first() previously landed on
    // whichever hook sorts first -- on the seeded fixture that is a legacy-event
    // hook whose edit form the current UI does not open, so the assertion timed
    // out. Editing a hook we create (valid event, unique name) is deterministic
    // and row-scoped.
    const hookName = `Playwright_Edit_${Date.now()}`
    await page.getByRole('button', { name: /\+ new hook/i }).click()
    await expect(page.getByPlaceholder(/hook name/i)).toBeVisible({ timeout: 3000 })
    await page.getByPlaceholder(/hook name/i).fill(hookName)
    await page.getByPlaceholder(/echo 'hook fired'/i).fill('echo "edit test"')
    await page.getByRole('button', { name: /^save$/i }).click()
    await expect(page.getByPlaceholder(/hook name/i)).not.toBeVisible({ timeout: 5000 })

    const row = page.getByRole('row').filter({ hasText: hookName })
    await expect(row).toBeVisible({ timeout: 3000 })

    // Open the edit form for our row and save an update.
    await row.getByRole('button', { name: /^edit$/i }).click()
    await expect(page.getByRole('button', { name: /^save$/i })).toBeVisible({ timeout: 3000 })
    const updatedName = `${hookName}_upd`
    await page.getByPlaceholder(/hook name/i).fill(updatedName)
    await page.getByRole('button', { name: /^save$/i }).click()
    await expect(page.getByPlaceholder(/hook name/i)).not.toBeVisible({ timeout: 5000 })
    await expect(page.getByText(updatedName).first()).toBeVisible({ timeout: 3000 })

    // Cleanup: delete the hook we created (window.confirm auto-accepted).
    page.on('dialog', d => d.accept())
    await page.getByRole('row').filter({ hasText: updatedName })
      .getByRole('button', { name: /^delete$/i }).click()
  })

  test('toggles hook enabled state', async ({ page }) => {
    await expect(page.getByRole('button', { name: /\+ new hook/i })).toBeVisible({ timeout: 10000 })

    await page.waitForTimeout(1000)

    // Find toggle switch
    const toggleSwitch = page
      .locator('button')
      .filter({ has: page.locator('span[class*="rounded-full"]') })
      .first()

    if (await toggleSwitch.isVisible()) {
      await toggleSwitch.click()
      
      // Wait for toggle to update
      await page.waitForTimeout(500)
    }
  })

  test('tests hook execution', async ({ page }) => {
    await expect(page.getByRole('button', { name: /\+ new hook/i })).toBeVisible({ timeout: 10000 })

    await page.waitForTimeout(1000)

    // Find first Test button
    const testButton = page.getByRole('button', { name: /^test$/i }).first()
    
    if (await testButton.isVisible()) {
      await testButton.click()

      // Should show test results (looks for "Test Result" heading)
      await expect(page.getByText(/test result/i)).toBeVisible({ timeout: 5000 })
    }
  })

  test('deletes a hook', async ({ page }) => {
    await expect(page.getByRole('button', { name: /\+ new hook/i })).toBeVisible({ timeout: 10000 })

    await page.waitForTimeout(1000)

    // Find first Delete button (✕)
    const deleteButton = page.getByRole('button', { name: /delete/i }).first()
    
    if (await deleteButton.isVisible()) {
      // Playwright automatically handles confirm dialogs
      page.on('dialog', dialog => dialog.accept())
      
      await deleteButton.click()

      // Wait for deletion
      await page.waitForTimeout(1000)
    }
  })

  test('changes event type', async ({ page }) => {
    await page.getByRole('button', { name: /\+ new hook/i }).click()

    await expect(page.getByPlaceholder(/hook name/i)).toBeVisible({ timeout: 3000 })

    // Find and change event select
    await pickFromDropdown(page, 'Event', 'PreToolUse')

    // Matcher placeholder should change to tool filter placeholder
    await expect(page.getByPlaceholder(/tool filter.*fs_write/i)).toBeVisible({ timeout: 2000 })
  })

  test('updates timeout value', async ({ page }) => {
    await page.getByRole('button', { name: /\+ new hook/i }).click()

    await expect(page.getByPlaceholder(/hook name/i)).toBeVisible({ timeout: 3000 })

    // Find timeout input
    const timeoutInput = page.locator('input[type="number"]')
    await timeoutInput.fill('60')

    // Verify value
    await expect(timeoutInput).toHaveValue('60')
  })
})
