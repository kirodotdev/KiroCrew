import { randomUUID } from 'node:crypto'
import { test, expect, type APIRequestContext, type Page } from '@playwright/test'

/**
 * The Bots page's phone bar: the Bot's identity stack (face and name chip,
 * centred on the viewport) must stay clear of the controls either side of it,
 * touch targets included. B035: with a remote crew, the crew switcher's chip
 * pushed the drawer toggle under the name chip, and a long name reached the
 * trail's search opener even without one. jsdom lays nothing out, so only a
 * real browser can check the boxes; `MembersPage.narrow.test.tsx` pins the
 * measured cap with Chromium's numbers and `InstanceTabBar.phone.test.tsx`
 * the icon-only crew chip.
 */

const TOUCH_TARGET_PX = 44
const CREW = {
  id: 'phone-bar-crew',
  name: 'devdesk',
  ssh_host: 'phone-bar-crew-alias',
  remote_port: 7777,
  local_port: 7801,
  ttl: '20h',
  remote_bin: '',
  connection_method: 'ssh',
  ssm_target: '',
  ssm_run_as: '',
  aws_profile: '',
  aws_region: '',
  // Not sticky: a live `status` alone lists the crew, and a sticky intent would
  // make the dashboard auto-connect a tunnel nothing here serves.
  was_connected: false,
  status: { instance_id: 'phone-bar-crew', state: 'connected', local_port: 7801, remote_port: 7777 },
}

// These writes must never target an operator gateway.
test.beforeEach(() => {
  expect(process.env.KIROCREW_E2E_EPHEMERAL, 'Use the isolated gateway E2E harness').toBe('1')
})

async function bot(request: APIRequestContext, name: string) {
  const response = await request.post('/api/agents', { data: { name, kiro_agent: 'kirocrew' } })
  expect(response.ok(), await response.text()).toBeTruthy()
  return name
}

/** One remote crew, or none: the real gateway has no tunnel to report. */
async function stubInstances(page: Page, remote: boolean) {
  await page.route(/127\.0\.0\.1:7801/, route => route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>pane</title>' }))
  await page.route(url => url.pathname.startsWith('/api/instances'), async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/instances') {
      const sso = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }
      await route.fulfill({ json: { active: true, instances: remote ? [CREW] : [], warm_set_cap: 5, sso } })
    } else if (/\/(connect|refresh-token)$/.test(path)) {
      await route.fulfill({ json: { ...CREW.status, token: 'stub' } })
    } else {
      await route.fulfill({ json: { ok: true } })
    }
  })
}

interface Box { left: number; right: number; top: number; bottom: number }

/** The bar's boxes: the name chip, and each visible control's touch target. */
async function barBoxes(page: Page) {
  return page.evaluate((hit) => {
    const box = (el: Element) => {
      const r = el.getBoundingClientRect()
      return { left: r.left, right: r.right, top: r.top, bottom: r.bottom }
    }
    const target = (el: Element) => {
      const r = el.getBoundingClientRect()
      const w = Math.max(r.width, hit) / 2
      const h = Math.max(r.height, hit) / 2
      const x = r.left + r.width / 2
      const y = r.top + r.height / 2
      return { left: x - w, right: x + w, top: y - h, bottom: y + h }
    }
    const chip = document.querySelector('[data-testid="member-pill-name"]')!.closest('.glass-shadow')!
    const cells = ['member-thread-header-lead', 'member-thread-header-trail']
    const controls = cells.flatMap(id => [...document.querySelectorAll(`[data-testid="${id}"] button`)])
      .filter(el => el.getBoundingClientRect().width > 0)
      .map(el => ({ id: el.getAttribute('data-testid') || el.getAttribute('aria-label') || el.tagName, box: target(el) }))
    return { chip: box(chip), controls }
  }, TOUCH_TARGET_PX)
}

const overlap = (a: Box, b: Box) =>
  Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left)) * Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top))

for (const viewport of [{ width: 390, height: 844 }, { width: 360, height: 740 }]) {
  test.describe(`phone bar at ${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport, isMobile: true, hasTouch: true })

    for (const remote of [false, true]) {
      for (const length of ['short', 'long'] as const) {
        test(`the identity stack clears the side controls, ${length} Bot name, ${remote ? 'one remote crew' : 'no remote crew'}`, async ({ page, request }) => {
          const suffix = randomUUID().slice(0, 6)
          const name = await bot(request, length === 'short' ? `Ivy${suffix}` : `phone-bar-long-bot-name-${suffix}`)
          await stubInstances(page, remote)
          await page.goto(`/members?member=${encodeURIComponent(name)}`)
          const pill = page.getByTestId('member-identity-pill')
          // The roster read and the page's lazy chunk come first.
          await expect(pill).toBeVisible({ timeout: 20_000 })
          await expect(page.getByTestId('member-pill-name')).toHaveText(name)
          // Every control either side must be on screen before the boxes mean
          // anything: the search opener mounts with the Bot's chat, after the bar.
          await expect(page.getByTestId('member-back')).toBeVisible()
          await expect(page.getByTestId('member-header-search')).toBeVisible()
          await expect(page.getByTestId('member-panel-toggle')).toBeVisible()
          if (remote) await expect(page.locator('.tb-crew-active-chip')).toBeVisible()
          // Polled: the cap is measured once the bar has laid out.
          await expect.poll(async () => {
            const { chip, controls } = await barBoxes(page)
            expect(controls.map(c => c.id)).toContain('member-back')
            return controls.filter(c => overlap(chip, c.box) > 0).map(c => `${c.id} overlaps the name chip by ${overlap(chip, c.box).toFixed(0)}px²`)
          }).toEqual([])
          if (remote) {
            // Icon-only on a phone: the crew's name is in its accessible name.
            const crewChip = page.locator('.tb-crew-active-chip')
            await expect(crewChip).toHaveText('')
            await expect(crewChip).toHaveAttribute('aria-label', /Local/)
          }
        })
      }
    }
  })
}
