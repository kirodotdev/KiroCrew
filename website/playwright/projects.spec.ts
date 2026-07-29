import { test, expect } from '@playwright/test'

const HARNESS_GATEWAY = !!process.env.KIROCREW_E2E_EPHEMERAL

/** Seed a planned run via from-chat API (does NOT execute). Returns task_id. */
async function seedPlannedRun(
  request: import('@playwright/test').APIRequestContext,
  name: string,
  steps: { title: string; description?: string }[] = [{ title: 'Step one' }, { title: 'Step two' }]
) {
  const res = await request.post('/api/taskrunner/from-chat', {
    data: { steps, original_input: `Test: ${name}` },
  })
  expect(res.ok()).toBeTruthy()
  const body = await res.json()
  expect(body.ok).toBe(true)
  expect(body.task_id).toBeTruthy()
  // Rename so it has a human-readable name in the sidebar
  const renameRes = await request.patch(
    `/api/taskrunner/${encodeURIComponent(body.task_id)}/name`,
    { data: { name } }
  )
  expect(renameRes.ok()).toBeTruthy()
  return body.task_id as string
}

/** Delete a run by task_id (best-effort). */
async function deleteRun(request: import('@playwright/test').APIRequestContext, taskId: string) {
  try {
    await request.delete(`/api/taskrunner/${encodeURIComponent(taskId)}`)
  } catch { /* best-effort */ }
}

test.describe('Projects (Task Runner) Page', () => {
  test('renders page header with title and subtitle', async ({ page }) => {
    await page.goto('/projects', { waitUntil: 'domcontentloaded' })
    await expect(page.getByText('Task Runner')).toBeVisible({ timeout: 10000 })
    await expect(page.getByTestId('page-subtitle')).toBeVisible()
  })

  test('compose panel shows three mode tabs', async ({ page }) => {
    await page.goto('/projects', { waitUntil: 'domcontentloaded' })
    await expect(page.getByText('Task Runner')).toBeVisible({ timeout: 10000 })

    // Three mode tabs are visible as buttons
    await expect(page.getByRole('button', { name: /Compose/i })).toBeVisible()
    await expect(page.getByRole('button', { name: /From Spec/i })).toBeVisible()
    await expect(page.getByRole('button', { name: /From YAML/i })).toBeVisible()
  })

  test('compose tab (default) has textarea and action buttons', async ({ page }) => {
    await page.goto('/projects', { waitUntil: 'domcontentloaded' })
    await expect(page.getByText('Task Runner')).toBeVisible({ timeout: 10000 })

    // The Compose tab textarea
    await expect(page.getByLabel('Describe your task')).toBeVisible()

    // Action buttons: Refine into Spec, Plan, Run
    await expect(page.getByRole('button', { name: /Refine into Spec/i })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Plan', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Run', exact: true })).toBeVisible()
  })

  test('From Spec tab shows spec textarea and file upload', async ({ page }) => {
    await page.goto('/projects', { waitUntil: 'domcontentloaded' })
    await expect(page.getByText('Task Runner')).toBeVisible({ timeout: 10000 })

    // Switch to From Spec tab
    await page.getByRole('button', { name: /From Spec/i }).click()

    // The spec textarea appears with its placeholder as aria-label
    const textarea = page.getByRole('textbox', { name: /Paste spec content/i })
    await expect(textarea).toBeVisible({ timeout: 5000 })

    // File upload input (use exact match to avoid the textarea's label collision)
    const fileInput = page.locator('input[type="file"][aria-label="Upload a file"]')
    await expect(fileInput).toBeAttached()

    // Run and Plan buttons are present (use exact to avoid matching "Task Runner" nav)
    await expect(page.getByRole('button', { name: 'Run', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Plan', exact: true })).toBeVisible()
  })

  test('From YAML tab shows yaml textarea and DAG banner', async ({ page }) => {
    await page.goto('/projects', { waitUntil: 'domcontentloaded' })
    await expect(page.getByText('Task Runner')).toBeVisible({ timeout: 10000 })

    // Switch to From YAML tab
    await page.getByRole('button', { name: /From YAML/i }).click()

    // YAML textarea with its placeholder
    const textarea = page.getByRole('textbox', { name: /Paste YAML workflow/i })
    await expect(textarea).toBeVisible({ timeout: 5000 })

    // YAML-specific banner mentioning DAG constraint
    await expect(page.getByText('YAML workflows bypass the LLM decomposer')).toBeVisible()
  })

  test('agent selector and workspace field are present', async ({ page }) => {
    await page.goto('/projects', { waitUntil: 'domcontentloaded' })
    await expect(page.getByText('Task Runner')).toBeVisible({ timeout: 10000 })

    // Agent label is visible
    await expect(page.getByText('Agent:')).toBeVisible()

    // Workspace field
    await expect(page.getByLabel('Workspace folder')).toBeVisible()
  })

  test('taskrunner status API returns available', async ({ request }) => {
    const res = await request.get('/api/taskrunner')
    expect(res.ok()).toBeTruthy()
    const body = await res.json()
    expect(body.available).toBe(true)
    // runs is an array (may not be empty due to other tests seeding data)
    expect(Array.isArray(body.runs)).toBe(true)
  })

  test('seeding a planned run makes it visible in the sidebar', async ({ page, request }) => {
    const taskName = `PW_Sidebar_${Date.now()}`
    const taskId = await seedPlannedRun(request, taskName)

    try {
      await page.goto('/projects', { waitUntil: 'domcontentloaded' })

      // The run appears in the sidebar with the expected aria-label
      const runBtn = page.getByRole('button', { name: `Open project ${taskName}` })
      await expect(runBtn).toBeVisible({ timeout: 10000 })

      // The "New Task" button is visible when sidebar has runs
      await expect(page.getByRole('button', { name: /New Task/i })).toBeVisible()
    } finally {
      await deleteRun(request, taskId)
    }
  })

  test('clicking a run shows its detail view', async ({ page, request }) => {
    const taskName = `PW_Detail_${Date.now()}`
    const taskId = await seedPlannedRun(request, taskName, [
      { title: 'First step', description: 'Do the first thing' },
      { title: 'Second step', description: 'Do the second thing' },
    ])

    try {
      await page.goto('/projects', { waitUntil: 'domcontentloaded' })

      // Click the run in the sidebar
      const runBtn = page.getByRole('button', { name: `Open project ${taskName}` })
      await expect(runBtn).toBeVisible({ timeout: 10000 })
      await runBtn.click()

      // The detail view header shows the run name as a rename-able span
      const headerName = page.locator('[aria-label="Rename project"]').first()
      await expect(headerName).toBeVisible({ timeout: 5000 })
      await expect(headerName).toHaveText(taskName)

      // Status badge shows "planned" (use exact: true to avoid sidebar substring match)
      await expect(page.getByText('planned', { exact: true })).toBeVisible()

      // Execute button is present for planned runs
      await expect(page.getByRole('button', { name: /Execute/i })).toBeVisible()
    } finally {
      await deleteRun(request, taskId)
    }
  })

  test('renaming a run updates the sidebar and header', async ({ page, request }) => {
    const taskName = `PW_Rename_${Date.now()}`
    const taskId = await seedPlannedRun(request, taskName)
    const newName = `PW_Renamed_${Date.now()}`

    try {
      await page.goto('/projects', { waitUntil: 'domcontentloaded' })

      // Click the run
      const runBtn = page.getByRole('button', { name: `Open project ${taskName}` })
      await expect(runBtn).toBeVisible({ timeout: 10000 })
      await runBtn.click()

      // Click the rename pencil icon to enter edit mode
      const renameIcon = page.locator('[aria-label="Rename project"][title="Rename project"]')
      await expect(renameIcon).toBeVisible({ timeout: 5000 })
      await renameIcon.click()

      // Rename input appears
      const renameInput = page.getByLabel('Project name')
      await expect(renameInput).toBeVisible()
      await renameInput.fill(newName)
      await renameInput.blur()

      // After blur, the API persists the rename and the sidebar updates
      const updatedBtn = page.getByRole('button', { name: `Open project ${newName}` })
      await expect(updatedBtn).toBeVisible({ timeout: 10000 })
    } finally {
      await deleteRun(request, taskId)
    }
  })
})
