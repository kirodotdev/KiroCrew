import { test, expect, Page } from '@playwright/test'

/**
 * Measured-y regression guard for the two hairlines that must share one screen
 * row at the bottom of the chat sidebar:
 *
 *   1. the nav rail's community row hairline — the `border-t` above the
 *      "Star us · Report issue" links, and
 *   2. the sessions panel's "Older Sessions" footer hairline — the divider
 *      directly above the collapse/expand header.
 *
 * PR #2884 aligned them by hand-matching two DIFFERENT padding budgets that
 * happen to sum to the same 44px offset below each card's own hairline: the
 * rail spends `8 + 2 + 24 + 10`, the footer spends `14 + 16 + 14`. The two
 * rows live in different components (App.tsx vs ChatSidebar.tsx) with different
 * DOM (icon + links vs clock + label + chevron), so there is no shared token
 * to derive one from the other — the only thing that ties them is the rendered
 * screen y, and only a browser can see that.
 *
 * A jsdom test cannot: jsdom has no layout engine, so it could assert the class
 * tokens but not that the two boxes land on the same pixel. That is exactly how
 * a rail-row change (a different community-button size, different section
 * padding) reopens the 4px drift with every check still green. This spec closes
 * that gap: it runs in the `E2E (stub ACP backend, offline)` gate against a
 * real browser and asserts the OUTPUT — `getBoundingClientRect()` of the two
 * hairline elements — so a broken alignment fails on measured pixels.
 *
 * Mutation-verified: changing the footer's `pt-[14px] pb-[14px]` to 16px moves
 * `olderHairlineY` by 4px while `railHairlineY` holds, and this spec reddens.
 *
 * The probe mirrors website/scripts/capture-older-sessions-header.mjs, the
 * manual capture harness that measures the same two elements; this automates
 * its COLLAPSED reading (the collapsed state is where the two hairlines
 * coincide — expanding the pane lifts the footer hairline by the pane height).
 *
 * No @needs-agent tag: this measures static layout, needs no model turn, and
 * belongs in the default credential-less green set. No seeding: the community
 * row and the Older Sessions footer both render on a bare /chat, and the wide
 * desktop viewport keeps the rail expanded so its community row is visible
 * (the row folds away under `max-h-0` while the rail is collapsed).
 */

const TOLERANCE = 0.5

type Probe = {
  railHairlineY: number
  olderHairlineY: number
  railBottomOffset: number
  olderBottomOffset: number
}
type Measurement = { probe: Probe | null; missing: string[] }

async function primeBrowser(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('mc-onboarded', '1')
  })
  // Wide enough that the nav rail stays expanded (its community row is hidden
  // under max-h-0 while collapsed) and tall enough that both cards sit on the
  // shell floor rather than being scrolled.
  await page.setViewportSize({ width: 1440, height: 900 })
}

async function measure(page: Page): Promise<Measurement> {
  return page.evaluate(() => {
    // .rail-community-links IS a div, so closest('div') would return the links
    // themselves; the hairline lives on their PARENT row (the `border-t` box).
    const railRow = document.querySelector('nav .rail-community-links')?.parentElement
    const nav = document.querySelector('nav')
    const sidebar = document.querySelector('.sidebar-inner')
    const older = document.querySelector('[aria-controls="history-pane"]')
    const divider = older?.previousElementSibling
    const missing = [
      !railRow && 'railRow',
      !nav && 'nav',
      !sidebar && 'sidebar',
      !older && 'older',
      !divider && 'divider',
    ].filter(Boolean) as string[]
    if (missing.length) return { probe: null, missing }
    const round = (n: number) => Math.round(n * 100) / 100
    const top = (el: Element) => round(el.getBoundingClientRect().top)
    // Bottom offset = distance from each card's own hairline down to the shell
    // floor. Both must be 44px (measured 45 here, the -1px being the card's
    // bottom border); asserting the offsets, not just the raw ys, keeps the
    // guard meaningful if the whole shell floor ever moves.
    const navBottom = round((nav as Element).getBoundingClientRect().bottom - 1)
    const sidebarBottom = round((sidebar as Element).getBoundingClientRect().bottom - 1)
    return {
      probe: {
        railHairlineY: top(railRow as Element),
        olderHairlineY: top(divider as Element),
        railBottomOffset: round(navBottom - top(railRow as Element)),
        olderBottomOffset: round(sidebarBottom - top(divider as Element)),
      },
      missing: [],
    }
  }) as Promise<Measurement>
}

/** The rail community row folds in via a max-height transition, so wait until
 *  two consecutive frames measure identically before asserting. This poll is
 *  also the wait for the elements themselves — a persistent `missing:` list in
 *  its failure message names exactly which selector never resolved. */
async function settleAndMeasure(page: Page): Promise<Probe> {
  let prev: Measurement = { probe: null, missing: ['<no measurement yet>'] }
  await expect
    .poll(async () => {
      const cur = await measure(page)
      const stable = cur.probe !== null && prev.probe !== null
        && JSON.stringify(cur.probe) === JSON.stringify(prev.probe)
      prev = cur
      return stable ? 'settled' : `missing: [${cur.missing.join(', ')}]`
    }, { timeout: 15000, message: 'rail community row and Older Sessions footer should render and settle (a persistent missing: list names the broken selector)' })
    .toBe('settled')
  return prev.probe as Probe
}

test.describe('Sidebar hairline alignment (measured y)', () => {
  test('the nav rail community hairline and the Older Sessions footer hairline share one screen row', async ({ page }) => {
    await primeBrowser(page)
    await page.goto('/chat')

    const m = await settleAndMeasure(page)
    const detail = `all: ${JSON.stringify(m)}`

    // Frame anchor — a difference-only assertion would hold vacuously at y=0 if
    // a regression left the nodes in the DOM without boxes. Both hairlines sit
    // low on an 900px-tall viewport.
    expect(m.railHairlineY, `rail hairline should have a real box (${detail})`).toBeGreaterThan(0)
    expect(m.olderHairlineY, `older hairline should have a real box (${detail})`).toBeGreaterThan(0)

    // The guard: the two hairlines land on the same screen y. This is the
    // identity nothing else holds — a rail-row change that shifts one and not
    // the other reddens here.
    expect(Math.abs(m.railHairlineY - m.olderHairlineY), `hairlines must share one screen y (${detail})`).toBeLessThanOrEqual(TOLERANCE)

    // And they get there for the same reason: an equal budget below each card's
    // own hairline. Asserting the offsets too catches a compensating regression
    // that moved BOTH cards by the same amount (raw ys would still match while
    // the layout silently changed).
    expect(Math.abs(m.railBottomOffset - m.olderBottomOffset), `hairline-to-floor offsets must match (${detail})`).toBeLessThanOrEqual(TOLERANCE)
  })
})
