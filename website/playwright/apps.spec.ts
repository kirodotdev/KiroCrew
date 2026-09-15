/**
 * E2E: the split App Store surfaces.
 *
 * Discover (/apps) and Library (/apps/library) are standalone pages (the
 * hybrid tabbed AppsPage and its SegmentedControl are gone): Discover is the
 * storefront (editorial layer + category rail + catalog), Library manages
 * installed apps. The static `library` segment registers ahead of the
 * /apps/:name installed-app catch-all and is a reserved app name server-side.
 *
 * Preconditions provided by the stub gateway:
 * - Several disabled builtins populate the Discover catalog
 * - Task Runner (name: "projects") is installed and enabled
 */

import { test, expect, type Page } from '@playwright/test'

/**
 * Catalog cards. The same aria-label is emitted by FeaturedSpotlight AND
 * AppListRow, so one app can legitimately match 2-3 times on
 * the Discover landing view -- always scope with .first() or assert on count.
 */
function browseCards(page: Page) {
  return page.locator('[role="button"][aria-label^="View details for"]')
}

/**
 * An installed app in the Library launchpad grid. Each app renders as a
 * LaunchpadTile whose root carries `data-testid="launchpad-tile-<name>"`
 * (the app NAME, e.g. "projects" -- not the display name). Anchoring on the
 * testid also keeps the lookup unambiguous: the tile's pin badge and
 * overflow trigger are buttons whose accessible names CONTAIN the display
 * name ("Pin Task Runner to the sidebar", "More actions for Task Runner"),
 * and an installed builtin also appears in the nav rail.
 */
function launchpadTile(page: Page, appName: string) {
  return page.getByTestId(`launchpad-tile-${appName}`)
}

/**
 * The tile face -- a real <button> (aria-label = display name, exact) that
 * opens the app or its detail page. Asserting on it pins both that the tile
 * exists AND that it wears the right accessible name, the same contract the
 * old InstalledAppCard name-button assertion carried.
 */
function tileFace(page: Page, appName: string, displayName: string) {
  return launchpadTile(page, appName).getByRole('button', { name: displayName, exact: true })
}

async function gotoDiscover(page: Page) {
  await page.goto('/apps', { waitUntil: 'domcontentloaded' })
  // Ready-signal is the catalog heading (category === 'all'): stable structure,
  // not subtitle prose a designer can reword.
  await expect(page.getByRole('heading', { name: 'All apps' })).toBeVisible({ timeout: 10000 })
}

async function gotoLibrary(page: Page) {
  await page.goto('/apps/library', { waitUntil: 'domcontentloaded' })
  // The Library search box is the page's stable ready-signal (its aria-label
  // is page-specific after the split).
  await expect(page.getByRole('textbox', { name: 'Search library' })).toBeVisible({ timeout: 10000 })
}

/**
 * Navigate straight to /apps/detail/:name. Asserting on the HTTP status is the
 * regression test for the SPA-fallback fix: before it, the gateway answered this
 * URL with 404 instead of the shell, so the route worked only via in-app
 * navigation.
 */
async function gotoDetail(page: Page, appName: string) {
  const res = await page.goto(`/apps/detail/${appName}`, { waitUntil: 'domcontentloaded' })
  expect(res?.status(), `GET /apps/detail/${appName} must be served the SPA shell, not 404`).toBe(200)
  // "Back to Apps" renders in both the found and not-found states.
  await expect(page.getByRole('button', { name: 'Back to Apps' })).toBeVisible({ timeout: 10000 })
}

test.describe('Discover Page — /apps', () => {
  test('renders the storefront: catalog heading, category rail, sort control', async ({ page }) => {
    await gotoDiscover(page)
    // Deliberately NOT asserting the "N apps" count line: that exact string is
    // rendered twice -- once by the CategoryRail as its source total, once as the
    // catalog result count -- so the locator is ambiguous, and .first() would
    // silently assert the rail's total instead of the catalog's.
    await expect(page.getByRole('button', { name: 'Add source' })).toBeVisible()
    await expect(page.getByRole('combobox', { name: 'Sort apps' })).toBeVisible()
  })

  test('renders catalog cards for the available builtins', async ({ page }) => {
    await gotoDiscover(page)
    const cards = browseCards(page)
    await expect(cards.first()).toBeVisible({ timeout: 10000 })
    expect(await cards.count()).toBeGreaterThan(0)
  })

  test('search filters the catalog down to its empty state', async ({ page }) => {
    await gotoDiscover(page)
    await expect(browseCards(page).first()).toBeVisible({ timeout: 10000 })

    const search = page.getByRole('textbox', { name: 'Search apps' })
    await search.fill('zzz_no_match_xyz')

    // A non-empty query also clears the editorial layer (showEditorial requires
    // !query.trim()), so every card unmounts -- not just the AppListRows.
    await expect(page.getByTestId('empty-state-title')).toHaveText('No matching apps', { timeout: 5000 })
    await expect(browseCards(page)).toHaveCount(0)
  })

  test('a stored legacy library tab redirects /apps to /apps/library once', async ({ page }) => {
    // The pre-split page persisted its tab in sessionStorage; DiscoverPage
    // translates a stored library value into a one-shot replace-redirect and
    // clears the key. addInitScript runs before the app boots, mirroring a
    // user upgrading with the old value persisted.
    await page.addInitScript(() => sessionStorage.setItem('appstore-tab', 'library'))
    await page.goto('/apps', { waitUntil: 'domcontentloaded' })
    await page.waitForURL('**/apps/library', { timeout: 10000 })
    await expect(page.getByRole('textbox', { name: 'Search library' })).toBeVisible({ timeout: 10000 })
  })
})

test.describe('Library Page — /apps/library', () => {
  test('lists the installed Task Runner app as a launchpad tile', async ({ page }) => {
    await gotoLibrary(page)
    await expect(launchpadTile(page, 'projects')).toBeVisible({ timeout: 10000 })
    await expect(tileFace(page, 'projects', 'Task Runner')).toBeVisible()
  })

  test('search narrows to matching installed apps', async ({ page }) => {
    await gotoLibrary(page)
    await expect(tileFace(page, 'projects', 'Task Runner')).toBeVisible({ timeout: 10000 })

    const search = page.getByRole('textbox', { name: 'Search library' })
    await search.fill('zzz_no_match_xyz')
    await expect(page.getByTestId('empty-state-title')).toHaveText('No matching apps', { timeout: 5000 })
    await expect(launchpadTile(page, 'projects')).toHaveCount(0)

    await search.clear()
    await expect(tileFace(page, 'projects', 'Task Runner')).toBeVisible({ timeout: 5000 })
  })

  test('page round-trip: Library and Discover are independently routable', async ({ page }) => {
    await gotoDiscover(page)
    await expect(page.getByRole('heading', { name: 'All apps' })).toBeVisible({ timeout: 5000 })

    await gotoLibrary(page)
    await expect(tileFace(page, 'projects', 'Task Runner')).toBeVisible({ timeout: 10000 })
    await expect(page.getByRole('heading', { name: 'All apps' })).toHaveCount(0)

    await gotoDiscover(page)
    await expect(page.getByRole('heading', { name: 'All apps' })).toBeVisible({ timeout: 5000 })
  })
})

test.describe('App Detail Page — /apps/detail/:name', () => {
  test('renders detail view for Task Runner', async ({ page }) => {
    await gotoDetail(page, 'projects')
    // The detail page shows the display name
    await expect(page.locator('text=Task Runner').first()).toBeVisible({ timeout: 10000 })
    await expect(page.getByRole('button', { name: 'Back to Apps' })).toBeVisible()
  })

  test('shows "App Not Found" for a nonexistent app', async ({ page }) => {
    await gotoDetail(page, 'this-app-does-not-exist-zyx')
    await expect(page.locator('text=App Not Found')).toBeVisible({ timeout: 10000 })
    await expect(page.getByRole('button', { name: 'Back to Apps' })).toBeVisible()
  })

  test('navigating from a Discover catalog card reaches the detail page', async ({ page }) => {
    await gotoDiscover(page)
    const firstCard = browseCards(page).first()
    await expect(firstCard).toBeVisible({ timeout: 10000 })
    await firstCard.click()

    await page.waitForURL('**/apps/detail/**', { timeout: 10000 })
    await expect(page.getByRole('button', { name: 'Back to Apps' })).toBeVisible({ timeout: 5000 })
  })

  test('detail page shows Details metadata card', async ({ page }) => {
    await gotoDetail(page, 'projects')
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

  test('the static /apps/library segment wins over the /apps/:name catch-all', async ({ page }) => {
    // Registration order is the contract this pins from the browser side; the
    // server-side half is the reserved-name refusal (reserved_app_name).
    await gotoLibrary(page)
    await expect(page.locator('text=is not installed')).toHaveCount(0)
  })
})

test.describe('Discover Updates sub-tab — /apps/-/updates', () => {
  // TODO(PR2+): the POPULATED updates list (UpdatesList rows, per-row Update,
  // Update All progress) is NOT covered here. The e2e gateway boots a fresh
  // data home where every builtin is installed at its bundled version — equal
  // to its registry version — so `updateAvailable` is false on every row and
  // `updatables` is always empty. Covering the populated state needs either a
  // gateway seed knob (install an app at an older version than the registry
  // advertises) or /api/apps/registry route interception; neither exists in
  // this harness and building one is out of scope for PR2. The populated list
  // and update flows are covered by vitest (useAppUpdates.test.tsx and the
  // DiscoverPage/UpdatesList suites) instead.

  test('deep-link renders the Updates tab active with the all-current empty state', async ({ page }) => {
    await page.goto('/apps/-/updates', { waitUntil: 'domcontentloaded' })

    // The route must resolve to DiscoverPage, not fall through to the
    // /apps/:name AppHost not-found ("is not installed").
    await expect(page.locator('text=is not installed')).toHaveCount(0)

    // `exact: true` doubles as the hidden-at-zero badge pin: with zero
    // updatables UnderlineTabs renders no count, so the accessible name is
    // exactly "Updates" — a leaked "Updates 0" badge would fail this locator.
    const updatesTab = page.getByRole('tab', { name: 'Updates', exact: true })
    await expect(updatesTab).toBeVisible({ timeout: 10000 })
    await expect(updatesTab).toHaveAttribute('aria-selected', 'true')

    // Pinned to the stub gateway's known state (zero updatables — see the
    // TODO above). If a future harness change seeds an updatable app, this
    // assertion fails loudly: extend coverage to the populated list then.
    await expect(page.getByTestId('empty-state-title')).toHaveText('Everything is up to date.', { timeout: 10000 })
  })

  test('sub-tab switching syncs the URL both ways', async ({ page }) => {
    await gotoDiscover(page)

    await page.getByRole('tab', { name: 'Updates', exact: true }).click()
    await page.waitForURL('**/apps/-/updates', { timeout: 10000 })
    await expect(page.getByTestId('empty-state-title')).toHaveText('Everything is up to date.', { timeout: 10000 })
    // Switching the sub-tab swaps the content region: the Featured catalog
    // heading must be unmounted, not merely below the fold.
    await expect(page.getByRole('heading', { name: 'All apps' })).toHaveCount(0)

    // The empty state's action returns to Featured and normalizes the URL
    // back to the bare /apps mount.
    await page.getByRole('button', { name: 'Browse Featured apps' }).click()
    await page.waitForURL(/\/apps$/, { timeout: 10000 })
    await expect(page.getByRole('heading', { name: 'All apps' })).toBeVisible({ timeout: 10000 })
    await expect(page.getByRole('tab', { name: 'Featured', exact: true })).toHaveAttribute('aria-selected', 'true')
  })
})
