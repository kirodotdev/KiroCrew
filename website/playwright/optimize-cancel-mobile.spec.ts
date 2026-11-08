import { test, expect, type Page, type Route } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { json, stubDashboardApi } from '../scripts/lib/stub-dashboard-api.mjs'

/**
 * On a phone, the "Optimizing prompt..." overlay shows a visible, tappable
 * Cancel while the optimize runs, and a tap gives back the draft, editable.
 *
 * Pearce hit this on iPhone Safari (#20003, handoff 11bb0734): the composer
 * showed only the status, with no way out. Touch screens get no title tooltip,
 * so under a coarse pointer the X grows into a labelled "Cancel" chip with a
 * 44px touch target. jsdom evaluates no media queries and computes no layout,
 * so only a real browser can check that the label is shown and the target is
 * big enough. ChatInput.optimizeCancel.test.tsx pins the abort and late-result
 * behaviour; this spec pins what a finger can see and hit.
 *
 * The phone runs at 390x844 and again at 390x480, the height left above the
 * soft keyboard, because the overlay must stay usable while the keyboard is up.
 * A desktop control keeps the 28px icon-only X with its tooltip.
 */

const catalog = (name: string) => JSON.parse(readFileSync(fileURLToPath(new URL(`../src/i18n/locales/${name}`, import.meta.url)), 'utf8'))
const manual = catalog('en.manual.json')
const generated = catalog('en.json')
const label = (key: string): string => {
  const value = manual.components.chatInput[key] ?? generated.components?.chatInput?.[key]
  if (typeof value !== 'string') throw new Error(`Missing English fixture label: components.chatInput.${key}`)
  return value
}
const CANCEL_NAME = label('cancel_optimizing')
const CANCEL_LABEL = label('cancel_optimizing_short')
const OPTIMIZING = label('optimizing_prompt')
const SLOT = 'optimize-cancel-fixture'
const DRAFT = 'look at the failing build and tell me what broke'
const TOUCH_TARGET_PX = 44

async function openComposer(page: Page, baseURL: string) {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/*', route => new URL(route.request().url()).origin === new URL(baseURL).origin
    ? route.continue() : route.abort('blockedbyclient'))
  const optimize = { requests: 0 }
  await stubDashboardApi(page, {
    localStorageEntries: { 'mc-lang': 'en', 'mc-active-slot': SLOT, 'mc-active-slot-chat': SLOT },
    slots: [{ key: SLOT, title: 'Build triage', running: false, messages: 2,
      agent: 'kirocrew', memory_mode: 'persistent', project: '', folder_id: '',
      modified: 1788780000, source_links: [], source_links_total: 0 }],
    extra: async (path: string, route: Route) => {
      if (path === `/api/chat/slots/${SLOT}`) {
        await json(route, { running: false, has_more: false, total: 2, queue: [], project: '', messages: [
          { role: 'user', ts: 1788780000, content: 'Hi' },
          { role: 'assistant', ts: 1788780030, content: 'Ready.' },
        ] })
      } else if (path === `/api/chat/slots/${SLOT}/source-links`) {
        await json(route, { links: [], total: 0 })
      } else if (path.startsWith('/api/optimizer/optimize')) {
        // Never answered: only the page's abort ends the request.
        optimize.requests++
      } else return false
      return true
    },
  })
  const response = await page.goto(`/?slot=${SLOT}`, { waitUntil: 'domcontentloaded' })
  expect(response?.ok()).toBe(true)
  // The composer is what this spec drives; the transcript is incidental.
  await expect(page.locator('textarea[data-composer-input]')).toBeVisible()
  return { errors, optimize }
}

async function startOptimize(page: Page, touch: boolean) {
  const composer = page.locator('textarea[data-composer-input]')
  await composer.fill(DRAFT)
  const optimizeButton = page.getByRole('button', { name: 'Optimize prompt' }).first()
  if (touch) await optimizeButton.tap()
  else await optimizeButton.click()
  await expect(page.getByText(OPTIMIZING)).toBeVisible()
  return composer
}

for (const height of [844, 480]) {
  test.describe(`Phone ${390}x${height}: optimize overlay Cancel`, () => {
    test.use({ viewport: { width: 390, height }, hasTouch: true, isMobile: true, reducedMotion: 'reduce' })

    test('shows a labelled 44px Cancel and a tap returns the draft, editable', async ({ page, baseURL }) => {
      const { errors, optimize } = await openComposer(page, baseURL!)
      const composer = await startOptimize(page, true)
      // The overlay paints on the mutation's start; the fetch can follow it.
      await expect.poll(() => optimize.requests).toBe(1)

      const cancel = page.getByRole('button', { name: CANCEL_NAME })
      await expect(cancel).toBeVisible()
      await expect(cancel.getByText(CANCEL_LABEL, { exact: true })).toBeVisible()
      await expect(cancel).toBeInViewport({ ratio: 1 })
      const box = (await cancel.boundingBox())!
      expect(box.height).toBeGreaterThanOrEqual(TOUCH_TARGET_PX)
      expect(box.width).toBeGreaterThanOrEqual(TOUCH_TARGET_PX)
      // A hit test at the button's centre lands on the button, not the overlay.
      const hit = await page.evaluate(([x, y]) => {
        const el = document.elementFromPoint(x, y)
        return !!el?.closest('.optimize-overlay-cancel')
      }, [box.x + box.width / 2, box.y + box.height / 2])
      expect(hit).toBe(true)

      await cancel.tap()
      await expect(cancel).toHaveCount(0)
      await expect(composer).not.toHaveAttribute('readonly')
      await expect(composer).toHaveValue(DRAFT)
      await composer.press('End')
      await composer.pressSequentially(' please')
      await expect(composer).toHaveValue(`${DRAFT} please`)
      expect(errors).toEqual([])
    })
  })
}

test.describe('Desktop: optimize overlay Cancel stays icon-only', () => {
  test.use({ viewport: { width: 1280, height: 800 }, reducedMotion: 'reduce' })

  test('shows the 28px X without the touch label', async ({ page, baseURL }) => {
    const { errors } = await openComposer(page, baseURL!)
    await startOptimize(page, false)
    const cancel = page.getByRole('button', { name: CANCEL_NAME })
    await expect(cancel).toBeVisible()
    await expect(cancel.getByText(CANCEL_LABEL, { exact: true })).toBeHidden()
    const box = (await cancel.boundingBox())!
    expect(Math.round(box.width)).toBe(28)
    expect(Math.round(box.height)).toBe(28)
    expect(errors).toEqual([])
  })
})
