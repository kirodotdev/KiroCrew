import { test, expect, type APIRequestContext, type Page } from '@playwright/test'

/**
 * Apps routes e2e — covers /apps, /apps/detail/:name, /apps/:name.
 *
 * The Apps page is a hybrid storefront (upstream #532): a "Discover" tab with an
 * editorial layer (FeaturedSpotlight + FeatureCards) over a category-railed
 * catalog, and a "Library" tab managing installed apps. It replaced the earlier
 * Installed/Browse tabs.
 *
 * Harness state:
 * - 1 installed & enabled builtin: "Task Runner" (name: "projects", route /projects)
 * - Several disabled builtins populate the Discover catalog
 * - Discover is the default tab on a fresh session (initialTab(), AppsPage.tsx:46)
 *
 * Tests assert the real harness state unconditionally. No conditional branches.
 *
 * FINDING: /apps/detail/:name is a client-side-only route — the server's SPA
 * fallback regex (_APPS_SPA_EXCLUDED_RE) treats "detail" as an app name and the
 * trailing segment as a sub-resource, returning 404 on direct navigation. Tests
 * navigate via the SPA shell rather than hitting the bare URL.
 */

type AppEntry = {
  name: string
  displayName: string
  enabled: boolean
  origin?: string
  manifest?: { description?: string; ui?: { pages?: { route: string }[] } }
}

/** Fetch the installed apps list from the API. */
async function listApps(request: APIRequestContext): Promise<AppEntry[]> {
  const res = await request.get('/api/apps')
  expect(res.ok()).toBeTruthy()
  return await res.json()
}

/**
 * Locate a tab segment. SegmentedControl exposes no aria-label, role, or
 * data-testid -- only title={label} -- so title is the sole stable handle. It is
 * also the only one that survives both hazards: compact mode drops the label
 * text from the DOM for inactive segments, and the Library segment renders a
 * count badge inside the button, which makes its accessible name "Library 1".
 */
function tab(page: Page, label: 'Discover' | 'Library') {
  return page.locator(`button[title="${label}"]`)
}

/**
 * Catalog cards. The same aria-label is emitted by FeaturedSpotlight,
 * FeatureCard AND AppListRow, so one app can legitimately match 2-3 times on
 * the Discover landing view -- always scope with .first() or assert on count.
 */
function browseCards(page: Page) {
  return page.locator('[role="button"][aria-label^="View details for"]')
}

/**
 * An installed app in the Library. InstalledAppCard's root is a bare div with no
 * testid or aria-label, but it renders the app name as a real <button> wired to
 * onDetail -- so role-scope it rather than matching loose text.
 *
 * Scoped to the #main-content landmark (App.tsx:1984): an installed builtin also
 * appears in the nav rail, so an unscoped role+name lookup resolves to 2
 * elements. This mirrors the convention capabilities.spec.ts already uses.
 */
function libraryCard(page: Page, displayName: string) {
  return page.locator('#main-content').getByRole('button', { name: displayName, exact: true })
}

async function gotoApps(page: Page) {
  await page.goto('/apps', { waitUntil: 'domcontentloaded' })
  // Ready-signal is the tab segment's title attribute, not the page subtitle:
  // subtitle copy is prose a designer can reword, title is a stable handle.
  await expect(tab(page, 'Discover')).toBeVisible({ timeout: 10000 })
}

/**
 * Navigate to a detail page via client-side routing (avoids the server 404 on
 * direct /apps/detail/:name — see FINDING above).
 */
async function gotoDetailViaRouter(page: Page, appName: string) {
  await gotoApps(page)
  await page.evaluate((name) => {
    window.history.pushState({}, '', `/apps/detail/${name}`)
    window.dispatchEvent(new PopStateEvent('popstate'))
  }, appName)
  // "Back to Apps" renders in both the found and not-found states.
  await expect(page.getByRole('button', { name: 'Back to Apps' })).toBeVisible({ timeout: 10000 })
}

test.describe('Apps Page — /apps', () => {
  test('renders the header, both tab segments, and the Discover catalog', async ({ page }) => {
    await gotoApps(page)
    await expect(tab(page, 'Discover')).toBeVisible({ timeout: 5000 })
    await expect(tab(page, 'Library')).toBeVisible()
    // Discover's two-column layout: the "All apps" heading (category === 'All'),
    // the CategoryRail, and the catalog's sort control.
    //
    // Deliberately NOT asserting the "N apps" count line: that exact string is
    // rendered twice -- once by the CategoryRail as its source total, once as the
    // catalog result count -- so the locator is ambiguous, and .first() would
    // silently assert the rail's total instead of the catalog's.
    await expect(page.getByRole('heading', { name: 'All apps' })).toBeVisible({ timeout: 5000 })
    await expect(page.getByRole('button', { name: 'Add source' })).toBeVisible()
    await expect(page.getByRole('combobox', { name: 'Sort apps' })).toBeVisible()
  })

  test('Discover is the default tab on a fresh session', async ({ page }) => {
    // initialTab() (AppsPage.tsx:46) returns 'discover' when sessionStorage has
    // no persisted tab. Playwright's storageState does not carry sessionStorage,
    // so every test starts fresh -- this is deterministic, not incidental.
    await gotoApps(page)
    await expect(page.getByRole('heading', { name: 'All apps' })).toBeVisible({ timeout: 5000 })
    await expect(browseCards(page).first()).toBeVisible({ timeout: 10000 })
  })

  test('Discover renders catalog cards for the available builtins', async ({ page }) => {
    await gotoApps(page)
    const cards = browseCards(page)
    await expect(cards.first()).toBeVisible({ timeout: 10000 })
    expect(await cards.count()).toBeGreaterThan(0)
  })

  test('Discover search filters the catalog down to its empty state', async ({ page }) => {
    await gotoApps(page)
    await expect(browseCards(page).first()).toBeVisible({ timeout: 10000 })

    const search = page.getByRole('textbox', { name: 'Search apps' })
    await search.fill('zzz_no_match_xyz')

    // A non-empty query also clears the editorial layer (showEditorial requires
    // !query.trim(), AppsPage.tsx:207), so every card unmounts -- not just the
    // AppListRows.
    await expect(page.getByText('No matching apps')).toBeVisible({ timeout: 5000 })
    await expect(browseCards(page)).toHaveCount(0)
  })

  test('Library tab lists the installed Task Runner app', async ({ page }) => {
    await gotoApps(page)
    await tab(page, 'Library').click()
    await expect(libraryCard(page, 'Task Runner')).toBeVisible({ timeout: 10000 })
  })

  test('Library search narrows to matching installed apps', async ({ page }) => {
    await gotoApps(page)
    await tab(page, 'Library').click()
    await expect(libraryCard(page, 'Task Runner')).toBeVisible({ timeout: 10000 })

    // One SearchInput serves both tabs; its placeholder switches per tab but the
    // aria-label stays "Search apps".
    const search = page.getByRole('textbox', { name: 'Search apps' })
    await search.fill('zzz_no_match_xyz')
    await expect(page.getByText('No matching apps')).toBeVisible({ timeout: 5000 })

    await search.clear()
    await expect(libraryCard(page, 'Task Runner')).toBeVisible({ timeout: 5000 })
  })

  test('tab round-trip returns to the Discover catalog', async ({ page }) => {
    await gotoApps(page)
    await expect(page.getByRole('heading', { name: 'All apps' })).toBeVisible({ timeout: 5000 })

    await tab(page, 'Library').click()
    await expect(libraryCard(page, 'Task Runner')).toBeVisible({ timeout: 10000 })
    await expect(page.getByRole('heading', { name: 'All apps' })).toHaveCount(0)

    await tab(page, 'Discover').click()
    await expect(page.getByRole('heading', { name: 'All apps' })).toBeVisible({ timeout: 5000 })
  })
})

test.describe('App Detail Page — /apps/detail/:name', () => {
  test('renders detail view for Task Runner', async ({ page }) => {
    await gotoDetailViaRouter(page, 'projects')
    // The detail page shows the display name
    await expect(page.locator('text=Task Runner').first()).toBeVisible({ timeout: 10000 })
    await expect(page.getByRole('button', { name: 'Back to Apps' })).toBeVisible()
  })

  test('shows "App Not Found" for a nonexistent app', async ({ page }) => {
    await gotoDetailViaRouter(page, 'this-app-does-not-exist-zyx')
    await expect(page.locator('text=App Not Found')).toBeVisible({ timeout: 10000 })
    await expect(page.getByRole('button', { name: 'Back to Apps' })).toBeVisible()
  })

  test('navigating from a Discover catalog card reaches the detail page', async ({ page }) => {
    await gotoApps(page)
    // Discover is the default tab, so the catalog is already on screen.
    const firstCard = browseCards(page).first()
    await expect(firstCard).toBeVisible({ timeout: 10000 })
    await firstCard.click()

    await page.waitForURL('**/apps/detail/**', { timeout: 10000 })
    await expect(page.getByRole('button', { name: 'Back to Apps' })).toBeVisible({ timeout: 5000 })
  })

  test('detail page shows Details metadata card', async ({ page }) => {
    await gotoDetailViaRouter(page, 'projects')
    await expect(page.locator('text=Task Runner').first()).toBeVisible({ timeout: 10000 })
    // The Details card heading
    await expect(page.locator('text=Details').first()).toBeVisible({ timeout: 5000 })
  })
})

test.describe('App Page — /apps/:name', () => {
  test('builtin app with native route redirects to its page', async ({ page }) => {
    // Task Runner (name: "projects") has route /projects — navigate to /apps/projects
    await page.goto('/apps/projects', { waitUntil: 'domcontentloaded' })
    // Should redirect to the native route /projects
    await page.waitForURL('**/projects', { timeout: 10000 })
  })

  test('nonexistent app shows not-found state via AppHost', async ({ page }) => {
    await page.goto('/apps/this-definitely-does-not-exist-zyx', { waitUntil: 'domcontentloaded' })
    // AppHost renders AppNotFound with subtitle containing "is not installed"
    await expect(page.locator('text=is not installed')).toBeVisible({ timeout: 10000 })
  })

  test('/apps/:name does NOT collide with /:builtinApp catch-all', async ({ page }) => {
    // React Router v6 ranks /apps/:name (two segments) higher than /:builtinApp
    // (single segment), so there is no precedence conflict.
    await page.goto('/apps/fake-precedence-test', { waitUntil: 'domcontentloaded' })
    // Wait for AppPage to finish loading — renders not-found for unknown apps
    await expect(page.locator('text=is not installed')).toBeVisible({ timeout: 10000 })
    const url = page.url()
    expect(url).not.toContain('/chat')
  })
})
