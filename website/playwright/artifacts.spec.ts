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
    // Click the artifact entry — it links to /artifacts/:slug. The card's open
    // control is a button laid over the whole card and named after the artifact,
    // so it (not the title text beneath it) is what takes the click.
    await page.getByRole('button', { name, exact: true }).first().click()
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

  test("an SVG artifact's own stylesheet draws its shapes and leaves the page alone, on screen and in print", async ({ page, request }) => {
    // Rules that reached the whole page when an SVG was drawn straight into it.
    const content = '<' + 'svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><style>'
      + '.st0 { fill: rgb(1, 2, 3) } '
      + 'body, body * { color: rgb(255, 0, 0) !important } '
      + ':host { display: block !important; position: fixed !important; inset: 0 !important; z-index: 2147483647 !important } '
      + 'svg { position: fixed !important; inset: 0 !important; width: 100vw !important; height: 100vh !important; z-index: 2147483647 !important } '
      + '@media print { body * { display: block !important } }'
      // Reads page state: fetches only while the page leaves --e2e-unset undefined.
      + 'rect { stroke: var(--e2e-unset, url("https://attacker.example/stylesheet")) } '
      + '</style><rect class="st0" width="40" height="40" mask="var(--e2e-unset, url(https://attacker.example/attribute))"/></svg>'
    const { slug, name } = await seedArtifact(request, { kind: 'svg', content })
    const leaks: string[] = []
    await page.route('https://attacker.example/**', route => {
      leaks.push(route.request().url())
      return route.fulfill({ status: 204 })
    })

    await page.goto(`/artifacts/${slug}`, { waitUntil: 'domcontentloaded' })
    const shape = page.getByTestId('viewer-backdrop').locator('rect')
    await expect(shape).toHaveCSS('fill', 'rgb(1, 2, 3)', { timeout: 10000 })
    const title = page.getByText(name).first()
    await expect(title).not.toHaveCSS('color', 'rgb(255, 0, 0)')
    // Neither the host nor a fixed <svg> lays the drawing over the page.
    await expect.poll(() => title.evaluate(el => {
      const box = el.getBoundingClientRect()
      return el.contains(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2))
    })).toBe(true)

    // Print the title alone, through the print preview's own markers
    // (components/markdownPanelPrint.ts): the SVG must stay out of the print.
    await title.evaluate(el => {
      el.setAttribute('data-print-root', '')
      document.documentElement.setAttribute('data-printing-preview', '')
    })
    await page.emulateMedia({ media: 'print' })
    await expect(shape).toBeHidden()
    await expect(title).toBeVisible()
    await expect(title).not.toHaveCSS('color', 'rgb(255, 0, 0)')

    await page.emulateMedia({ media: 'screen' })
    await page.goto('/artifacts', { waitUntil: 'domcontentloaded' })
    await page.getByPlaceholder('Filter by name, slug, description…').fill(slug)
    await expect(page.locator('rect.st0')).toHaveCSS('fill', 'rgb(1, 2, 3)', { timeout: 10000 })
    expect(leaks).toEqual([])
  })

  test('an SVG artifact keeps its size: a view-box-only one fills the viewer and the card, a percent one fits, a wide one scrolls', async ({ page, request }) => {
    const content = '<' + 'svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 600"><rect class="vb" width="800" height="600"/></svg>'
    const { slug } = await seedArtifact(request, { kind: 'svg', content })

    await page.goto(`/artifacts/${slug}`, { waitUntil: 'domcontentloaded' })
    const shape = page.getByTestId('viewer-backdrop').locator('rect.vb')
    await expect.poll(async () => (await shape.boundingBox())?.width ?? 0, { timeout: 10000 }).toBeGreaterThan(100)

    await page.goto('/artifacts', { waitUntil: 'domcontentloaded' })
    await page.getByPlaceholder('Filter by name, slug, description…').fill(slug)
    await expect.poll(async () => (await page.locator('rect.vb').boundingBox())?.width ?? 0, { timeout: 10000 }).toBeGreaterThan(100)

    // A drawing sized in percent fits the viewer's height, as it did.
    const fit = await seedArtifact(request, {
      kind: 'svg',
      content: '<' + 'svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" viewBox="0 0 800 100"><rect class="fit" width="800" height="100"/></svg>',
    })
    await page.goto(`/artifacts/${fit.slug}`, { waitUntil: 'domcontentloaded' })
    const fitBackdrop = page.getByTestId('viewer-backdrop')
    await expect(fitBackdrop.locator('rect.fit')).toHaveCount(1, { timeout: 10000 })
    await expect.poll(() => fitBackdrop.evaluate(el => {
      const svg = el.querySelector('[data-guide-untrusted]')?.shadowRoot?.querySelector('svg')
      return !!svg && Math.abs(svg.getBoundingClientRect().height - el.clientHeight) <= el.clientHeight * 0.2
    })).toBe(true)

    // A drawing that does not shrink overflows the viewer; its far side stays in reach by scrolling.
    const wide = await seedArtifact(request, {
      kind: 'svg',
      content: '<' + 'svg xmlns="http://www.w3.org/2000/svg" width="4000" height="200" style="flex-shrink: 0"><rect class="wide" width="4000" height="200"/></svg>',
    })
    await page.goto(`/artifacts/${wide.slug}`, { waitUntil: 'domcontentloaded' })
    const backdrop = page.getByTestId('viewer-backdrop')
    await expect(backdrop.locator('rect.wide')).toHaveCount(1, { timeout: 10000 })
    await expect.poll(() => backdrop.evaluate(el => {
      const host = el.querySelector('[data-guide-untrusted]')
      return [el, host].some(box => !!box && box.scrollWidth - box.clientWidth > 1000)
    })).toBe(true)
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
