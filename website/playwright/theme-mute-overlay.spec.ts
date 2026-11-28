import { test, expect, type Page } from '@playwright/test'

/**
 * On a phone the theme-sounds mute toggle rides the composer dock (#43431). A
 * pack overlay on the `above` layer that takes pointer events paints in the
 * shell's decor slot at OVERLAY_Z_MAX; the toggle must stay above it, or the
 * overlay swallows the tap and Mute stops working while audio plays.
 *
 * Only a real browser hit-tests, so this is an E2E spec, not a unit test. The
 * theme is stubbed (list, detail, active colour and the overlay page), so the
 * spec needs no installed pack and changes no gateway state.
 */

const SLUG = 'mute-probe'
const OVERLAY_ID = 'trap'
// A translucent tint that never hides its own frame.
const OVERLAY_HTML = '<!doctype html><html><body style="margin:0;background:rgba(160,0,0,.2)"></body></html>'

async function stubTheme(page: Page) {
  // The active colour: keep the gateway's own reply, point it at the stub pack.
  for (const url of [/\/api\/config\/theme$/, /\/api\/theme\/boot$/]) {
    await page.route(url, async route => {
      if (route.request().method() !== 'GET') return route.fallback()
      const real = await route.fetch()
      const body = await real.json()
      return route.fulfill({ response: real, json: { ...body, color: `custom-${SLUG}`, onboarded: true } })
    })
  }
  await page.route(/\/api\/themes$/, async route => {
    if (route.request().method() !== 'GET') return route.fallback()
    const real = await route.fetch()
    const body = await real.json()
    const entry = { slug: SLUG, name: 'Mute probe', emoji: '🔈', created_at: '', source: 'installed', level: 2 }
    return route.fulfill({ response: real, json: { ...body, themes: [...(body.themes ?? []), entry] } })
  })
  await page.route(new RegExp(`/api/themes/${SLUG}$`), route => route.fulfill({
    json: {
      slug: SLUG, name: 'Mute probe', emoji: '🔈', level: 2, source: 'installed', dark: {}, light: {},
      assets: {
        hasAudio: true,
        audio: { triggers: {}, ambient: null },
        overlays: [{
          id: OVERLAY_ID, position: 'fullscreen', zIndex: 44, pointerEvents: true,
          layer: 'above', animation: 'continuous', trigger: 'continuous',
        }],
      },
    },
  }))
  await page.route(new RegExp(`/api/theme/${SLUG}/overlay/${OVERLAY_ID}$`), route => route.fulfill({ contentType: 'text/html', body: OVERLAY_HTML }))
  await page.addInitScript((slug) => {
    localStorage.setItem('mc-color-theme', `custom-${slug}`)
    localStorage.setItem(`mc-theme-consent-${slug}`, 'consented-v2')
    localStorage.setItem(`mc-theme-experience-${slug}`, JSON.stringify({ sounds: true, overlays: true, persona: false }))
  }, SLUG)
}

test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, serviceWorkers: 'block' })

test('a pointer-events overlay does not swallow a tap on the phone mute toggle', async ({ page }) => {
  // Reduced motion unmounts animated overlays; this case needs one mounted.
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  await stubTheme(page)
  await page.goto('/chat', { waitUntil: 'load' })
  const mute = page.getByRole('button', { name: /^mute theme sounds/i })
  await expect(mute).toBeVisible({ timeout: 20000 })
  // The toggle rides the dock on a phone, so this case is the dock slot.
  await expect(page.getByTestId('theme-mute-slot').getByRole('button')).toHaveCount(1)
  const overlay = page.locator(`#theme-decor-slot iframe[title*="${OVERLAY_ID}"]`)
  await expect(overlay).toBeVisible()

  const box = (await mute.boundingBox())!
  const x = box.x + box.width / 2
  const y = box.y + box.height / 2
  // The overlay really covers the toggle and takes pointer events, so the
  // assertion below is not vacuous.
  const frame = (await overlay.boundingBox())!
  expect(x > frame.x && x < frame.x + frame.width && y > frame.y && y < frame.y + frame.height).toBe(true)
  await expect(overlay).toHaveCSS('pointer-events', 'auto')

  // A real tap at the toggle's centre: whatever is topmost receives it.
  await page.touchscreen.tap(x, y)
  await expect(page.getByRole('button', { name: /^unmute theme sounds/i })).toBeVisible()
})
