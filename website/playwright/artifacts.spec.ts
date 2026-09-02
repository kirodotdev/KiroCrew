import { test, expect, type APIRequestContext } from '@playwright/test'
import { pickFromDropdown } from './helpers/dropdown'

const HARNESS_GATEWAY = !!process.env.KIROCREW_E2E_EPHEMERAL

const UNIQUE = `pw-${Date.now()}`

/** Create an artifact via the REST API and return its slug.
 * Uses a unique slug per call to avoid 409 conflicts between tests. */
async function seedArtifact(request: APIRequestContext, overrides?: {
  name?: string; slug?: string; content?: string; kind?: string; tags?: string[]
}) {
  // POST/PATCH /api/artifacts fire ArtifactKnowledgeSync.on_change
  // (artifact_ingest.py:230, gated on knowledge.auto_ingest_artifacts which
  // DEFAULTS TO TRUE). That chains to pipeline.ingest_file() ->
  // delete_items_batch (ingestion.py:341) -> store.py:494, which runs an
  // UNSCOPED sweep: DELETE FROM entities WHERE id NOT IN (SELECT entity_id FROM
  // mentions) AND ... -- destroying pre-existing orphan entities in a real
  // developer's Knowledge Library. Seeding an artifact is therefore not a local
  // operation, so it requires the ephemeral harness gateway.
  test.skip(
    !HARNESS_GATEWAY,
    'seeding artifacts requires the ephemeral harness gateway: artifact ingestion triggers a global orphan-entity sweep (store.py:494)',
  )
  const slug = overrides?.slug ?? `e2e-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  const name = overrides?.name ?? `Test Artifact ${slug}`
  const res = await request.post('/api/artifacts', {
    data: {
      name,
      slug,
      content: overrides?.content ?? `<h1>Hello from ${slug}</h1><p>E2E test content.</p>`,
      kind: overrides?.kind ?? 'widget',
      description: 'Seeded by Playwright artifacts spec',
      tags: overrides?.tags ?? ['e2e-artifacts', 'pw-test'],
    },
  })
  // 201 = created, 200 = dedup bumped existing (same source_path).
  expect(res.status(), `seed artifact failed: ${await res.text()}`).toBeLessThan(300)
  const body = await res.json()
  return { slug: body.slug as string, name }
}

test.describe('Artifacts Page — /artifacts', () => {
  test('renders the page with header and filter controls', async ({ page }) => {
    await page.goto('/artifacts', { waitUntil: 'domcontentloaded' })
    // PageHeader with title "Artifacts"
    await expect(page.getByText('Artifacts', { exact: true }).first()).toBeVisible({ timeout: 10000 })
    // Search input present
    await expect(page.getByPlaceholder('Filter by name, slug, description…')).toBeVisible()
    // Kind filter dropdown
    await expect(page.getByRole('combobox', { name: 'Filter by kind' })).toBeVisible()
  })

  test('seeded artifact appears in the list via search', async ({ page, request }) => {
    const { slug, name } = await seedArtifact(request)
    await page.goto('/artifacts', { waitUntil: 'domcontentloaded' })
    const search = page.getByPlaceholder('Filter by name, slug, description…')
    await search.fill(slug)
    await expect(page.getByText(name)).toBeVisible({ timeout: 10000 })
  })

  test('kind filter narrows results to matching artifacts', async ({ page, request }) => {
    const { slug, name } = await seedArtifact(request, { kind: 'widget' })
    await page.goto('/artifacts', { waitUntil: 'domcontentloaded' })
    // Set kind filter to widget
    // The kind filter stores 'widget' but renders 'kind: widget'.
    await pickFromDropdown(page, 'Filter by kind', 'kind: widget')
    // Search for our specific artifact
    const search = page.getByPlaceholder('Filter by name, slug, description…')
    await search.fill(slug)
    await expect(page.getByText(name)).toBeVisible({ timeout: 10000 })
  })

  test('clicking an artifact navigates to its detail page', async ({ page, request }) => {
    const { slug, name } = await seedArtifact(request)
    await page.goto('/artifacts', { waitUntil: 'domcontentloaded' })
    const search = page.getByPlaceholder('Filter by name, slug, description…')
    await search.fill(slug)
    await expect(page.getByText(name)).toBeVisible({ timeout: 10000 })
    // Click the artifact entry — it links to /artifacts/:slug
    await page.getByText(name).first().click()
    await expect(page).toHaveURL(new RegExp(`/artifacts/${slug}`), { timeout: 5000 })
  })
})

test.describe('Artifact Detail Page — /artifacts/:slug', () => {
  test('displays artifact name and slug in header', async ({ page, request }) => {
    const { slug, name } = await seedArtifact(request)
    await page.goto(`/artifacts/${slug}`, { waitUntil: 'domcontentloaded' })
    await expect(page.getByText(name)).toBeVisible({ timeout: 10000 })
    await expect(page.getByText(`Artifact: ${slug}`)).toBeVisible()
  })

  test('tags are rendered as individual badges', async ({ page, request }) => {
    const { slug } = await seedArtifact(request, { tags: ['alpha-tag', 'beta-tag'] })
    await page.goto(`/artifacts/${slug}`, { waitUntil: 'domcontentloaded' })
    await expect(page.getByText('alpha-tag', { exact: true })).toBeVisible({ timeout: 10000 })
    await expect(page.getByText('beta-tag', { exact: true })).toBeVisible()
  })

  test('round-trip: update content creates version 2', async ({ page, request }) => {
    const { slug, name } = await seedArtifact(request)
    // Update the artifact via API — snapshot=true bumps the version
    const updated = `<h1>Updated</h1><p>${slug} v2</p>`
    const patchRes = await request.patch(`/api/artifacts/${encodeURIComponent(slug)}`, {
      data: { content: updated, snapshot: true },
    })
    expect(patchRes.status()).toBe(200)
    const patchBody = await patchRes.json()
    expect(patchBody.version).toBe(2)

    // Navigate to the detail page
    await page.goto(`/artifacts/${slug}`, { waitUntil: 'domcontentloaded' })
    await expect(page.getByText(name)).toBeVisible({ timeout: 10000 })

    // Verify via the versions API that both v1 and v2 exist
    const versionsRes = await request.get(`/api/artifacts/${encodeURIComponent(slug)}/versions`)
    expect(versionsRes.status()).toBe(200)
    const versions = await versionsRes.json()
    expect(versions.versions).toContain(1)
    expect(versions.versions).toContain(2)
  })

  test('404 page for non-existent slug', async ({ page }) => {
    await page.goto('/artifacts/non-existent-slug-xyz', { waitUntil: 'domcontentloaded' })
    // The detail page shows an error message containing the slug
    await expect(page.getByText('artifact not found: non-existent-slug-xyz')).toBeVisible({ timeout: 10000 })
  })
})

test.describe('Artifact Deploy Page — /deploy', () => {
  test('renders with page header and stat cards', async ({ page }) => {
    await page.goto('/deploy', { waitUntil: 'domcontentloaded' })
    // Use exact match on the heading text to avoid strict-mode collision
    await expect(page.getByText('Artifact Deploy', { exact: true })).toBeVisible({ timeout: 10000 })
    await expect(page.getByText('Active Deployments')).toBeVisible()
    await expect(page.getByText('Ready to Deploy')).toBeVisible()
  })
})

test.describe('/artifacts/deploy redirect', () => {
  test('redirects to /deploy', async ({ page }) => {
    await page.goto('/artifacts/deploy', { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveURL(/\/deploy$/, { timeout: 5000 })
    await expect(page.getByText('Artifact Deploy', { exact: true })).toBeVisible({ timeout: 10000 })
  })
})
