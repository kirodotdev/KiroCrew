import { test, expect } from '@playwright/test'
import { randomUUID } from 'crypto'
import { pickFromDropdown } from './helpers/dropdown'

const HARNESS_GATEWAY = !!process.env.KIROCREW_E2E_EPHEMERAL

/**
 * Ids of knowledge items seeded by the current test, drained by afterEach.
 * Module scope is safe here: playwright.config.ts runs a single worker in CI and
 * afterEach drains the list after every test, so entries never leak across tests.
 */
const seededItemIds: string[] = []

/** Generate an item id and register it for afterEach cleanup. */
function trackedId(): string {
  const id = randomUUID()
  seededItemIds.push(id)
  return id
}

test.describe('Knowledge Page E2E Tests', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/knowledge', { waitUntil: 'domcontentloaded' })
  })

  // Cleanup deletes ONLY the ids this run created, never a title-prefix sweep.
  // A `title.startsWith('Playwright_')` match cannot distinguish our seed data
  // from a real item a user happens to have named that way, so pointing this
  // suite at a live gateway to debug a failure would destroy their data. Every
  // seeded item carries a client-generated id (trackedId), so scoping teardown
  // to that set is both safe on any gateway and still effective in CI.
  test.afterEach(async ({ request }) => {
    const ids = seededItemIds.splice(0, seededItemIds.length)
    for (const id of ids) {
      try {
        await request.delete(`/api/knowledge/items/${id}`)
      } catch {
        // best-effort teardown
      }
    }
  })

  test('renders the onboarding empty state and stats bar on fresh fixture', async ({ page }) => {
    // Regression: isEmpty used to require `!statusFilter`, but statusFilter
    // starts at 'active', so this block was unreachable for every user. The
    // fresh fixture has 0 items and no filters applied, which is exactly the
    // state the onboarding copy exists for.
    await expect(page.getByTestId('knowledge-onboarding')).toBeVisible({ timeout: 10000 })
    // exact: true — the onboarding heading also contains "Knowledge Library"
    // ("Welcome to the Knowledge Library"), so a substring match resolves to two
    // elements and trips strict mode.
    await expect(page.getByText('Knowledge Library', { exact: true })).toBeVisible({ timeout: 10000 })
    // The stats bar renders outside the empty-state branch, once /stats returns.
    // With the minimal fixture items and entities are 0, but sources may be >0
    // due to auto_ingest_artifacts.
    await expect(page.getByText('0 items')).toBeVisible({ timeout: 10000 })
    await expect(page.getByText('0 entities')).toBeVisible()
    await expect(page.getByText('0 relations')).toBeVisible()
    await expect(page.getByText(/\d+ sources/)).toBeVisible()
  })

  test('a filter matching nothing shows the empty list, not the onboarding state', async ({ page, request }) => {
    test.skip(
      !HARNESS_GATEWAY,
      'seeding knowledge items requires the ephemeral harness gateway: delete_item runs a global orphan-entity sweep (store.py:472)',
    )
    // The onboarding branch REPLACES the filter bar, so rendering it while a
    // filter is applied would leave no control to clear that filter with. One
    // seeded item plus a filter that matches nothing must keep the bar mounted.
    const itemId = trackedId()
    const importRes = await request.post('/api/knowledge/import', {
      data: {
        items: [{
          id: itemId,
          title: `Playwright_FilterGuard_${randomUUID().slice(0, 8)}`,
          content: 'Content for the filter-vs-onboarding guard.',
          item_type: 'document',
          status: 'active',
          namespace: 'default',
        }],
        entities: [],
        relations: [],
      },
    })
    expect(importRes.ok()).toBeTruthy()

    await page.goto('/knowledge', { waitUntil: 'domcontentloaded' })
    const typeSelect = page.getByRole('combobox', { name: 'Filter by type' })
    await expect(typeSelect).toBeVisible({ timeout: 10000 })
    // 'policy' is a valid ITEM_TYPES value that the seeded 'document' item
    // cannot match, so the list goes to 0 results with a filter applied.
    await pickFromDropdown(page, 'Filter by type', 'policy')

    await expect(page.getByTestId('knowledge-onboarding')).toHaveCount(0)
    await expect(typeSelect).toBeVisible()
  })

  test('tabs switch between list, graph, and sources', async ({ page }) => {
    // Wait for the page to fully load (stats bar visible means data fetched)
    await expect(page.getByText('0 items')).toBeVisible({ timeout: 10000 })

    // Click "Graph View" tab
    await page.getByRole('button', { name: /Graph View/i }).click()
    // The graph canvas (lazy-loaded) should appear or a loading skeleton.
    // In graph view, the stats bar hint at the bottom changes.
    await expect(page.getByText('/ to search')).not.toBeVisible()

    // Click "Sources" tab
    await page.getByRole('button', { name: /Sources/i }).click()
    // Sources tab shows source management UI — the "+ Add Source" button is always visible
    await expect(page.getByRole('button', { name: /\+ Add Source/i })).toBeVisible({ timeout: 5000 })

    // Click back to "List View"
    await page.getByRole('button', { name: /List View/i }).click()
    await expect(page.getByText('/ to search')).toBeVisible({ timeout: 5000 })
  })

  test('help dialog opens and closes', async ({ page }) => {
    // exact: true — the onboarding heading also contains "Knowledge Library"
    // ("Welcome to the Knowledge Library"), so a substring match resolves to two
    // elements and trips strict mode (see the same guard above).
    await expect(page.getByText('Knowledge Library', { exact: true })).toBeVisible({ timeout: 10000 })

    // Click the Help button
    await page.getByRole('button', { name: /Help/i }).click()

    // The dialog should appear with keyboard shortcuts
    await expect(page.getByRole('dialog')).toBeVisible({ timeout: 5000 })
    await expect(page.getByText('Keyboard Shortcuts')).toBeVisible()
    await expect(page.getByText('Focus search')).toBeVisible()

    // Close via the Close button (aria-label="Close")
    await page.getByLabel('Close').click()
    await expect(page.getByRole('dialog')).not.toBeVisible()
  })

  test('import bundle creates items visible via API', async ({ request }) => {
    test.skip(
      !HARNESS_GATEWAY,
      'seeding knowledge items requires the ephemeral harness gateway: delete_item runs a global orphan-entity sweep (store.py:472)',
    )
    const suffix = randomUUID().slice(0, 8)
    const itemId = trackedId()
    const itemTitle = `Playwright_Import_${suffix}`

    // Import a knowledge bundle with one item (id is REQUIRED by store.import_bundle)
    const importRes = await request.post('/api/knowledge/import', {
      data: {
        items: [
          {
            id: itemId,
            title: itemTitle,
            content: 'This is test content for the Playwright e2e test.',
            item_type: 'document',
            status: 'active',
            namespace: 'default',
          },
        ],
        entities: [],
        relations: [],
      },
    })
    expect(importRes.ok()).toBeTruthy()
    const importBody = await importRes.json()
    expect(importBody.items_imported).toBeGreaterThanOrEqual(1)

    // Verify item appears in the API list
    const listRes = await request.get('/api/knowledge/items?limit=100&status=active')
    expect(listRes.ok()).toBeTruthy()
    const listBody = await listRes.json()
    const found = listBody.items.find((i: { title: string }) => i.title === itemTitle)
    expect(found).toBeTruthy()
    expect(found.content).toContain('Playwright e2e test')
  })

  test('imported item renders in the list view', async ({ page, request }) => {
    test.skip(
      !HARNESS_GATEWAY,
      'seeding knowledge items requires the ephemeral harness gateway: delete_item runs a global orphan-entity sweep (store.py:472)',
    )
    const suffix = randomUUID().slice(0, 8)
    const itemId = trackedId()
    const itemTitle = `Playwright_Visible_${suffix}`

    // Seed an item via the import API
    const importRes = await request.post('/api/knowledge/import', {
      data: {
        items: [
          {
            id: itemId,
            title: itemTitle,
            content: 'Visible content for the Playwright render test.',
            item_type: 'document',
            status: 'active',
            namespace: 'default',
          },
        ],
        entities: [],
        relations: [],
      },
    })
    expect(importRes.ok()).toBeTruthy()

    // Reload the page to pick up the new data
    await page.goto('/knowledge', { waitUntil: 'domcontentloaded' })

    // With an imported item, the stats bar should now show >= 1 item.
    // The source-first view groups by source -- our item has no source_id so it
    // falls under the "no source" bucket. The source group is auto-expanded when
    // it's the only one.
    await expect(page.getByText(itemTitle)).toBeVisible({ timeout: 10000 })
  })

  test('search filters items by query', async ({ page, request }) => {
    test.skip(
      !HARNESS_GATEWAY,
      'seeding knowledge items requires the ephemeral harness gateway: delete_item runs a global orphan-entity sweep (store.py:472)',
    )
    const suffix = randomUUID().slice(0, 8)
    const targetId = trackedId()
    const decoyId = trackedId()
    const targetTitle = `Playwright_SearchTarget_${suffix}`
    const decoyTitle = `Playwright_SearchDecoy_${suffix}`
    const searchTerm = `UniqueSearchTerm_${suffix}`

    // Seed two items with different content
    await request.post('/api/knowledge/import', {
      data: {
        items: [
          {
            id: targetId,
            title: targetTitle,
            content: `${searchTerm} is in this document.`,
            item_type: 'document',
            status: 'active',
            namespace: 'default',
          },
          {
            id: decoyId,
            title: decoyTitle,
            content: 'This document has completely different content with no match.',
            item_type: 'document',
            status: 'active',
            namespace: 'default',
          },
        ],
        entities: [],
        relations: [],
      },
    })

    // Reload
    await page.goto('/knowledge', { waitUntil: 'domcontentloaded' })
    // Wait for items to appear (source group expands when single)
    await expect(page.getByText(targetTitle)).toBeVisible({ timeout: 10000 })

    // Type in the search box and press Enter
    const searchInput = page.getByPlaceholder('Search knowledge... (press Enter to search)')
    await searchInput.fill(searchTerm)
    await searchInput.press('Enter')

    // Target should be visible, decoy should not (search matches content)
    await expect(page.getByText(targetTitle)).toBeVisible({ timeout: 10000 })
    await expect(page.getByText(decoyTitle)).not.toBeVisible({ timeout: 5000 })
  })

  test('stats endpoint returns correct counts after import', async ({ request }) => {
    test.skip(
      !HARNESS_GATEWAY,
      'seeding knowledge items requires the ephemeral harness gateway: delete_item runs a global orphan-entity sweep (store.py:472)',
    )
    const suffix = randomUUID().slice(0, 8)
    const itemId = trackedId()
    const entityId = randomUUID()

    // Seed items with entities (id is required for both items and entities)
    const importRes = await request.post('/api/knowledge/import', {
      data: {
        items: [
          {
            id: itemId,
            title: `Playwright_StatsItem_${suffix}`,
            content: 'Stats test content.',
            item_type: 'document',
            status: 'active',
            namespace: 'default',
          },
        ],
        entities: [
          {
            id: entityId,
            name: `TestEntity_${suffix}`,
            entity_type: 'concept',
            description: 'A test entity.',
          },
        ],
        relations: [],
      },
    })
    expect(importRes.ok()).toBeTruthy()

    // Verify stats
    const statsRes = await request.get('/api/knowledge/stats')
    expect(statsRes.ok()).toBeTruthy()
    const stats = await statsRes.json()
    expect(stats.items).toBeGreaterThanOrEqual(1)
    expect(stats.entities).toBeGreaterThanOrEqual(1)
  })

  test('filter by type select narrows items', async ({ page, request }) => {
    test.skip(
      !HARNESS_GATEWAY,
      'seeding knowledge items requires the ephemeral harness gateway: delete_item runs a global orphan-entity sweep (store.py:472)',
    )
    const suffix = randomUUID().slice(0, 8)
    const docId = trackedId()
    const runbookId = trackedId()
    const docTitle = `Playwright_TypeDoc_${suffix}`
    const runbookTitle = `Playwright_TypeRunbook_${suffix}`

    // Seed two items with different types
    await request.post('/api/knowledge/import', {
      data: {
        items: [
          {
            id: docId,
            title: docTitle,
            content: 'A document item.',
            item_type: 'document',
            status: 'active',
            namespace: 'default',
          },
          {
            id: runbookId,
            title: runbookTitle,
            content: 'A runbook item.',
            item_type: 'runbook',
            status: 'active',
            namespace: 'default',
          },
        ],
        entities: [],
        relations: [],
      },
    })

    await page.goto('/knowledge', { waitUntil: 'domcontentloaded' })
    // Wait for items to load
    await expect(page.getByText(docTitle)).toBeVisible({ timeout: 10000 })

    // Select type filter "runbook"
    await pickFromDropdown(page, 'Filter by type', 'runbook')

    // Runbook should remain, document should disappear
    await expect(page.getByText(runbookTitle)).toBeVisible({ timeout: 10000 })
    await expect(page.getByText(docTitle)).not.toBeVisible({ timeout: 5000 })
  })
})
