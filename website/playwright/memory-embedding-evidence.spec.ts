import { writeFile } from 'node:fs/promises'
import { test, expect, type APIRequestContext, type Page, type TestInfo } from '@playwright/test'

// Real Memory-tab embedding states, photographed against a DEDICATED gateway
// that test/e2e/test_memory_ui_evidence.py boots per scenario. Nothing here
// mocks HTTP, edits frontend state or short-circuits a backend check: every
// state is produced by the production status endpoint reading the gateway's
// own config.json, SQLite stores and download manager. The driver selects one
// scenario per gateway with `--grep`; each is tagged @memory-evidence so the
// shared-gateway suite (test_playwright_e2e.py) never collects it.
//
// The driver sets an absolute per-scenario output under its temporary home,
// or KIROCREW_MEMORY_UI_EVIDENCE_DIR in CI (runner.temp/memory-embedding-evidence).
// The uploader collects memory-v2-embedding-*.png and the manifest from that
// dedicated directory, not the shared suite's website/test-results directory.

const SCENARIO = process.env.KIROCREW_E2E_MEMORY_SCENARIO || ''

type Status = {
  model_id?: string
  model_dim?: number
  model_source?: string
  model_active?: boolean
  setup_step?: string
  download_step?: string
  download_attempt?: number
  setup_error_code?: string
  // Raw backend prose: read only to assert it is NOT on the page; never written to the manifest.
  setup_error?: string
  setup_warning_code?: string
  can_retry?: boolean
  reembed?: { step?: string }
  repair?: {
    generation?: string
    pending_invalidation?: number
    pending_vectors?: number
    deferred_stores?: number
    unknown_scope?: boolean
  }
}

// Only non-secret, assertion-bearing fields leave the runner. No cookie, token,
// home path or raw backend prose.
function publicStatus(status: Status) {
  return {
    model_id: status.model_id,
    model_dim: status.model_dim,
    model_source: status.model_source,
    model_active: status.model_active,
    setup_step: status.setup_step,
    download_step: status.download_step,
    download_attempt: status.download_attempt,
    setup_error_code: status.setup_error_code,
    setup_warning_code: status.setup_warning_code,
    can_retry: status.can_retry,
    reembed_step: status.reembed?.step,
    repair: status.repair && {
      pending_invalidation: status.repair.pending_invalidation,
      pending_vectors: status.repair.pending_vectors,
      deferred_stores: status.repair.deferred_stores,
      unknown_scope: status.repair.unknown_scope,
    },
  }
}

async function status(request: APIRequestContext): Promise<Status> {
  const response = await request.get('/api/memory/embedding-status')
  expect(response.ok(), await response.text()).toBeTruthy()
  return await response.json() as Status
}

async function shot(page: Page, testInfo: TestInfo, name: string) {
  const viewport = page.viewportSize()
  expect(viewport && viewport.width < 2000 && viewport.height < 2000, 'evidence stays under 2000px per side').toBeTruthy()
  await page.screenshot({ path: testInfo.outputPath(`memory-v2-embedding-${name}.png`), fullPage: false, animations: 'disabled' })
  return `memory-v2-embedding-${name}.png`
}

async function manifest(testInfo: TestInfo, scenario: string, before: Status, images: string[], extra: Record<string, unknown> = {}) {
  await writeFile(testInfo.outputPath('memory-v2-embedding-evidence.json'), JSON.stringify({
    scenario,
    title: testInfo.title,
    status: testInfo.status,
    retry: testInfo.retry,
    checkoutSha: process.env.GITHUB_SHA || 'unavailable',
    prHeadSha: process.env.VOICE_EVIDENCE_HEAD || 'unavailable',
    runId: process.env.GITHUB_RUN_ID || 'unavailable',
    runAttempt: process.env.GITHUB_RUN_ATTEMPT || 'unavailable',
    scope: 'Dedicated ephemeral gateway, real /api/memory/embedding-status, config.json and SQLite; fake ACP model backend; no HTTP mock or frontend state edit.',
    images,
    apiStatus: publicStatus(before),
    ...extra,
  }, null, 2), 'utf8')
}

test.beforeEach(async ({ page }) => {
  expect(process.env.KIROCREW_E2E_EPHEMERAL, 'Use the isolated gateway E2E harness').toBe('1')
  expect(SCENARIO, 'the Python driver names the scenario this gateway was prepared for').not.toBe('')
  await page.emulateMedia({ reducedMotion: 'reduce' })
})

async function openMemoryTab(page: Page) {
  await page.goto('/settings/overview?view=memory')
  await expect(page.getByTestId('embed-model-card')).toBeVisible()
}

test('missing configured model with inherited vectors: pointer, field error, Apply disabled @memory-evidence', async ({ page, request }, testInfo) => {
  expect(SCENARIO).toBe('missing-custom-legacy')
  const before = await status(request)
  expect(before.model_source).toBe('custom')
  expect(before.setup_error_code).toBe('model_path_not_found')
  expect(before.setup_warning_code).toBe('legacy_embedding_vectors')
  expect(before.can_retry).toBe(false)

  await openMemoryTab(page)
  const card = page.getByTestId('embed-model-card')
  // Field-level error is the ONE place the fault is stated in full.
  const fieldError = card.getByTestId('embed-model-path-status-error')
  await expect(fieldError).toContainText('No file at that path.')
  await expect(page.getByText('No file at that path.', { exact: false })).toHaveCount(1)
  // The raw backend prose never reaches the page.
  await expect(page.getByText('points at a file', { exact: false })).toHaveCount(0)
  // The field still holds the configured path, so the button is a reapply of
  // the configured path: labelled as such, and disabled by the path error.
  const apply = card.getByRole('button', { name: 'Rebuild memory vectors', exact: true })
  await expect(apply).toBeDisabled()
  await expect(card.getByRole('button', { name: 'Apply model', exact: true })).toHaveCount(0)
  // The legacy path warning owns the only settings jump; the field owns the error.
  await expect(page.getByTestId('embedding-setup-error-pointer')).toHaveCount(0);
  // The legacy warning swaps "reapply" for "fix the path first".
  const warning = page.getByTestId('embedding-setup-warning')
  await expect(warning).toContainText('Fix the model path first, then apply it to rebuild them.')
  // No standing rebuild is claimed for a model that cannot load.
  await expect(page.getByTestId('embed-model-repair-status')).toHaveCount(0)
  const settingsLink = page.getByRole('link', { name: 'Open embedding model settings', exact: true })
  await expect(settingsLink).toHaveCount(1)
  await expect(warning.getByRole('link')).toHaveAttribute('href', '#embed-model-path')
  await settingsLink.click()
  await expect(card.locator('#embed-model-path')).toBeFocused()
  await warning.scrollIntoViewIfNeeded()
  const images = [await shot(page, testInfo, 'missing-custom-legacy')]
  await fieldError.scrollIntoViewIfNeeded()
  await expect(apply).toBeVisible()
  await expect(apply).toBeDisabled()
  images.push(await shot(page, testInfo, 'missing-custom-model-field'))
  await manifest(testInfo, SCENARIO, before, images)
})

test('missing configured model and no inherited vectors: short pointer to the field, error stated once @memory-evidence', async ({ page, request }, testInfo) => {
  expect(SCENARIO).toBe('missing-custom-pointer')
  const before = await status(request)
  expect(before.model_source).toBe('custom')
  expect(before.setup_error_code).toBe('model_path_not_found')
  // No legacy ids configured, so no inherited-vectors warning to own the link.
  expect(before.setup_warning_code ?? '').toBe('')
  expect(before.can_retry).toBe(false)

  await openMemoryTab(page)
  const card = page.getByTestId('embed-model-card')
  // The field states the fault in full, exactly once on the page…
  const fieldError = card.getByTestId('embed-model-path-status-error')
  await expect(fieldError).toContainText('No file at that path.')
  await expect(page.getByText('No file at that path.', { exact: false })).toHaveCount(1)
  // …so the Vector Memory card renders the SHORT pointer, not the message or the path.
  const pointer = page.getByTestId('embedding-setup-error-pointer')
  await expect(pointer).toHaveCount(1)
  await expect(pointer).toContainText('memory uses keyword search meanwhile')
  await expect(pointer).not.toContainText('No file at that path.')
  // The destination is named once, by the link; the prose stops at the consequence.
  await expect(pointer).not.toContainText('Fix the path')
  await expect(pointer.getByText(/settings/i)).toHaveCount(1)
  // The full path belongs to the field, not the error pointer. The existing
  // model disclosure legitimately names the custom file; require that ONE
  // occurrence to be the disclosure, rather than forbidding the identifier.
  const configuredPath = await card.locator('#embed-model-path').inputValue()
  expect(configuredPath).toMatch(/[/\\\\]moved-away-custom\.gguf$/)
  await expect(pointer).not.toContainText('moved-away-custom.gguf')
  await expect(fieldError).not.toContainText('moved-away-custom.gguf')
  await expect(page.getByText(configuredPath, { exact: false })).toHaveCount(0)
  const disclosure = page.getByText('moved-away-custom.gguf', { exact: false })
  await expect(disclosure).toHaveCount(1)
  await expect(disclosure).toHaveText(before.model_dim ? `moved-away-custom.gguf · ${before.model_dim}-dim` : 'moved-away-custom.gguf')
  expect(await disclosure.getAttribute('title')).toContain(configuredPath)
  await expect(page.getByTestId('embedding-setup-warning')).toHaveCount(0)
  await expect(page.getByTestId('embedding-setup-diagnostic')).toHaveCount(0)
  await expect(page.getByText('points at a file', { exact: false })).toHaveCount(0)
  // The pointer owns the only settings jump, and it lands on the path field.
  const settingsLink = page.getByRole('link', { name: 'Open embedding model settings', exact: true })
  await expect(settingsLink).toHaveCount(1)
  await expect(pointer.getByRole('link')).toHaveAttribute('href', '#embed-model-path')
  await settingsLink.click()
  await expect(card.locator('#embed-model-path')).toBeFocused()
  // Configured path still in the field, same disabled reapply.
  await expect(card.getByRole('button', { name: 'Rebuild memory vectors', exact: true })).toBeDisabled()
  await expect(page.getByTestId('embed-model-repair-status')).toHaveCount(0)
  await pointer.scrollIntoViewIfNeeded()
  const images = [await shot(page, testInfo, 'missing-custom-pointer')]
  await manifest(testInfo, SCENARIO, before, images)
})

test('a known configured model that is not serving reads configured-not-active with a muted badge @memory-evidence', async ({ page, request }, testInfo) => {
  expect(SCENARIO).toBe('configured-inactive')
  const before = await status(request)
  // Identity is known (bundled model id + width) while nothing serves vectors:
  // the file was never downloaded on this gateway.
  expect(before.model_id).toBeTruthy()
  expect(before.model_dim).toBeGreaterThan(0)
  expect(before.model_active).toBe(false)
  expect(before.reembed?.step).not.toBe('running')

  await openMemoryTab(page)
  const header = page.getByTestId('embed-model-active')
  await expect(header).toHaveAttribute('data-state', 'inactive')
  await expect(header).toContainText(`Configured: ${before.model_id} · ${before.model_dim}d · not active`)
  await expect(header).not.toContainText('Active model unknown')
  await expect(header).not.toContainText('Active:')
  const badge = page.getByTestId('embed-model-active-badge')
  await expect(badge).toHaveAttribute('data-state', 'inactive')
  await expect(badge).toHaveText('bundled')
  // Muted provenance, not the success colour a serving model earns.
  await expect(badge).toHaveClass(/text-\[var\(--muted\)\]/)
  await expect(badge).not.toHaveClass(/text-ok/)
  await expect(badge).not.toHaveClass(/text-danger/)
  // Inactive is never dressed up as a rebuild in flight, nor as a fault: no
  // progress bar, no rebuild line, no error under the field, no raw prose.
  await expect(page.getByRole('progressbar', { name: 'Re-embedding memories…' })).toHaveCount(0)
  await expect(page.getByText('Re-embedding memories…', { exact: true })).toHaveCount(0)
  await expect(page.getByTestId('embed-model-repair-status')).toHaveCount(0)
  await expect(page.getByTestId('embed-model-path-status-error')).toHaveCount(0)
  await expect(page.getByTestId('embed-model-card').getByRole('alert')).toHaveCount(0)
  if (before.setup_error) await expect(page.getByTestId('embed-model-card')).not.toContainText(before.setup_error)
  await expect(page.getByTestId('embed-model-card')).not.toContainText('Keyword search still works')
  // The Vector Memory card's Embeddings stat tile on the same tab agrees with
  // the header: not active, in the same muted colour, never a guessed "model
  // loading" warning and never the success colour.
  const tile = page.getByTestId('embeddings-stat-badge')
  await expect(tile).toHaveAttribute('data-state', 'inactive')
  await expect(tile).toHaveText('not active')
  await expect(tile).toHaveClass(/text-\[var\(--muted\)\]/)
  await expect(tile).not.toHaveClass(/text-warn/)
  await expect(tile).not.toHaveClass(/text-ok/)
  await expect(page.getByText('model loading', { exact: true })).toHaveCount(0)
  // Nothing configured differs from the bundled default, so the button offers a reapply.
  const reapply = page.getByTestId('embed-model-card').getByRole('button', { name: 'Rebuild memory vectors', exact: true })
  await expect(reapply).toBeVisible()
  await expect(reapply).toBeEnabled()
  await header.scrollIntoViewIfNeeded()
  const images = [await shot(page, testInfo, 'configured-inactive')]
  // The tile lives on the Vector Memory card above the header, outside the
  // viewport once the header is framed: scroll it in and photograph it on its
  // own so the muted "not active" tile is evidence, not only an assertion.
  await tile.scrollIntoViewIfNeeded()
  await expect(tile).toBeInViewport()
  images.push(await shot(page, testInfo, 'configured-inactive-stat-tile'))
  await header.scrollIntoViewIfNeeded()
  // The reapply's confirm modal borrows the card's own neutral title and names
  // the action it confirms; its body describes reloading the configured model.
  // Opened and CANCELLED: the capture is of the modal, and no apply is sent to
  // this gateway (its bundled file is absent, so an apply would be a real,
  // failing model load).
  await reapply.click()
  const dialog = page.getByRole('dialog', { name: 'Embedding Model', exact: true })
  await expect(dialog).toBeVisible()
  await expect(dialog).not.toContainText('Change the embedding model?')
  await expect(dialog.getByRole('button', { name: 'Rebuild memory vectors', exact: true })).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Change model', exact: true })).toHaveCount(0)
  await expect(dialog).toContainText('This reloads the configured model and clears and rebuilds the stored memory vectors.')
  await expect(dialog).not.toContainText('new one')
  await expect(dialog).toContainText('Keyword search keeps working throughout.')
  images.push(await shot(page, testInfo, 'configured-inactive-reapply-confirm'))
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  // Cancel submitted nothing: the gateway reports no apply in flight.
  const after = await status(request)
  expect(after.reembed?.step ?? 'idle').not.toMatch(/^(applying|running)$/)
  expect(after.model_active).toBe(false)

  // The driver blocks only this gateway's checkpoint directory after startup.
  // Create a real run through the owner API; no response or UI state is mocked.
  const started = await request.post('/api/workflows/run', { data: {
    name: 'Checkpoint storage evidence',
    source: 'META = {"name": "checkpoint-evidence"}\nasync def workflow(ctx):\n    return {"evidence": "checkpoint-result-kept"}\n',
  } })
  expect(started.ok(), await started.text()).toBeTruthy()
  const { run_id: runId } = await started.json()
  expect(runId).toMatch(/^wf_/)
  const storageError = 'Workflow checkpoint could not be saved. Copy needed results before restart; check storage access and free space. A later checkpoint retries automatically.'
  await expect.poll(async () => {
    const response = await request.get(`/api/workflows/runs/${encodeURIComponent(runId)}`)
    expect(response.ok()).toBeTruthy()
    const run = await response.json()
    return { status: run.status, error: run.error, errorCode: run.error_code, result: run.result }
  }).toEqual({ status: 'finished', error: storageError, errorCode: 'workflow_checkpoint_failed', result: { evidence: 'checkpoint-result-kept' } })
  await page.goto('/workflows')
  await page.getByRole('button', { name: 'Runs', exact: true }).click()
  await page.getByRole('button', { name: /Checkpoint storage evidence/ }).click()
  const storageNotice = page.getByTestId('workflow-run-tree-error')
  await expect(storageNotice).toContainText('Progress could not be saved.')
  await expect(storageNotice).not.toContainText(storageError)
  const diagnostic = page.getByTestId('workflow-checkpoint-diagnostic')
  await expect(diagnostic).not.toHaveAttribute('open')
  await expect(page.getByText(/"evidence": "checkpoint-result-kept"/)).toBeVisible()
  await storageNotice.scrollIntoViewIfNeeded()
  images.push(await shot(page, testInfo, 'workflow-checkpoint-storage-warning'))
  await diagnostic.getByText('View details', { exact: true }).click()
  await expect(diagnostic.locator('pre')).toBeVisible()
  await expect(diagnostic.locator('pre')).toHaveText(storageError)
  images.push(await shot(page, testInfo, 'workflow-checkpoint-storage-diagnostic'))
  await manifest(testInfo, SCENARIO, before, images, {
    reapplyConfirm: { opened: true, cancelled: true, reembedStepAfter: after.reembed?.step ?? 'idle' },
    checkpointWarning: { runId, status: 'finished', warningObserved: true, resultPreserved: true },
  })
})

test('a standing rebuild reports vectors, open invalidation and deferred stores as three counts @memory-evidence', async ({ page }, testInfo) => {
  expect(SCENARIO).toBe('deferred-repair')
  // Observe the actual response shared by both cards, not a second independent
  // API read whose inventory may differ before the next 30-second refresh.
  const initialStatus = page.waitForResponse(response =>
    new URL(response.url()).pathname === '/api/memory/embedding-status' &&
    response.request().method() === 'GET')
  await openMemoryTab(page)
  const response = await initialStatus
  expect(response.ok(), await response.text()).toBeTruthy()
  const before = await response.json() as Status
  expect(before.repair?.generation).toBe('evidence-request-1')
  expect(before.repair?.unknown_scope).toBe(false)
  expect(before.reembed?.step).toBe('deferred')
  const vectors = before.repair?.pending_vectors ?? 0
  const invalidation = before.repair?.pending_invalidation ?? 0
  const deferred = before.repair?.deferred_stores ?? 0
  // The driver seeds each unit independently so the sum would be a visibly
  // different number from any single count.
  expect(vectors).toBeGreaterThan(0)
  expect(deferred).toBeGreaterThan(0)
  expect(vectors).not.toBe(deferred)

  const line = page.getByTestId('embed-model-repair-status')
  await expect(line).toHaveCount(1)
  const vectorClause = vectors === 1 ? '1 memory still needs a new vector' : `${vectors} memories still need a new vector`
  const invalidationClause = invalidation === 1 ? '1 open store still holds old vectors to clear' : `${invalidation} open stores still hold old vectors to clear`
  const deferredClause = deferred === 1 ? '1 closed or unavailable store will be rebuilt when it next opens' : `${deferred} closed or unavailable stores will be rebuilt when they next open`
  // Only the units the API reports as non-zero are named, joined the way the
  // UI language joins a list (`Intl.ListFormat`, en: "A, B, and C" / "A and B").
  const clauses = [vectors ? vectorClause : '', invalidation ? invalidationClause : '', deferred ? deferredClause : ''].filter(Boolean)
  const expected = new Intl.ListFormat('en', { type: 'conjunction', style: 'long' }).format(clauses)
  await expect(line).toHaveText(`The rebuild after the model change is still in progress across all memory stores: ${expected}.`)
  if (!invalidation) await expect(line).not.toContainText('open store')
  await expect(line).not.toContainText(/\b0 /)
  await expect(line).toHaveAttribute('role', 'status')
  const card = page.getByTestId('embed-model-card')
  await expect(card.getByRole('progressbar')).toHaveCount(0)
  await expect(line).not.toContainText(`${vectors + invalidation + deferred} memories`)
  await expect(page.getByText('Some memory stores could not be checked', { exact: false })).toHaveCount(0)
  // The standing rebuild is status, not a fault: the muted reassurance sits
  // under it exactly once, no alert is raised and no failure hint is shown.
  const reassurance = card.getByText('Keyword search still works. Safe to leave this page.', { exact: true })
  await expect(reassurance).toHaveCount(1)
  await expect(reassurance).toBeVisible()
  await expect(reassurance).toHaveClass(/text-muted/)
  await expect(reassurance).not.toHaveClass(/text-danger/)
  await expect(card.getByRole('alert')).toHaveCount(0)
  await expect(card.getByText('Re-embedding failed', { exact: true })).toHaveCount(0)
  await expect(page.getByText('The model could not be applied', { exact: false })).toHaveCount(0)
  await line.scrollIntoViewIfNeeded()
  const images = [await shot(page, testInfo, 'deferred-repair')]
  await manifest(testInfo, SCENARIO, before, images, { counts: { vectors, invalidation, deferred } })
})

test('a bundled download that exhausts its retries shows the terminal notice with collapsible details @memory-evidence', async ({ page, request }, testInfo) => {
  expect(SCENARIO).toBe('download-failed')
  // Three real attempts against a loopback URL nothing serves: connection
  // refused, 60s backoff, refused, 120s backoff, refused, terminal. The retry
  // policy is the production constant; only the wait here is sized to it.
  test.setTimeout(480_000)
  const kicked = await request.post('/api/memory/enable-embeddings', { data: {} })
  expect(kicked.ok(), await kicked.text()).toBeTruthy()
  expect((await kicked.json()).status).toBe('downloading')
  const seen = new Set<string>()
  await expect.poll(async () => {
    const now = await status(request)
    if (now.download_step) seen.add(now.download_step)
    return now.download_step
  }, { timeout: 420_000, intervals: [2_000, 5_000] }).toBe('failed')
  // `waiting_retry` is the intermediate state a premature capture confuses
  // with the terminal one; requiring it in the trace proves the attempts ran.
  expect(Array.from(seen)).toContain('waiting_retry')
  const before = await status(request)
  expect(before.download_step).toBe('failed')
  expect(before.download_attempt).toBe(3)
  expect(before.setup_step).toBe('error')
  expect(before.setup_error_code).toBe('model_download_failed')
  expect(before.can_retry).toBe(true)

  await openMemoryTab(page)
  const notice = page.getByText('The bundled model could not be downloaded. Check your network or proxy, then retry.', { exact: true })
  await expect(notice).toBeVisible()
  await expect(page.getByText('Retrying download', { exact: false })).toHaveCount(0)
  const details = page.getByTestId('embedding-setup-diagnostic')
  await expect(details).toBeVisible()
  const summary = details.locator('summary')
  await expect(summary).toHaveText('View details')
  // Raw transport prose is behind the fold, never in the notice.
  expect(await details.evaluate(node => (node as HTMLDetailsElement).open)).toBe(false)
  await notice.scrollIntoViewIfNeeded()
  const images = [await shot(page, testInfo, 'download-failed-collapsed')]
  await summary.click()
  expect(await details.evaluate(node => (node as HTMLDetailsElement).open)).toBe(true)
  const diagnostic = (await details.textContent()) || ''
  expect(diagnostic.length).toBeGreaterThan('View details'.length)
  // The raw transport prose the API reports is inside the fold and nowhere else:
  // not in the notice, not under the Embedding Model card's field.
  if (before.setup_error) {
    expect(diagnostic).toContain(before.setup_error)
    await expect(notice).not.toContainText(before.setup_error)
    await expect(page.getByTestId('embed-model-card')).not.toContainText(before.setup_error)
  }
  await expect(page.getByTestId('embed-model-path-status-error')).toHaveCount(0)
  await expect(page.getByTestId('embed-model-repair-status')).toHaveCount(0)
  images.push(await shot(page, testInfo, 'download-failed-expanded'))
  await summary.click()
  expect(await details.evaluate(node => (node as HTMLDetailsElement).open)).toBe(false)
  await manifest(testInfo, SCENARIO, before, images, { downloadStepsObserved: Array.from(seen) })
})
