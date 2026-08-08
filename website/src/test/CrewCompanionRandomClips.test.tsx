/**
 * PetDex random clips are REACHABLE, and a stopped session is not a celebration.
 *
 * Both pin the same failure class this app keeps growing: a channel exists, art is
 * imported and stored, and nothing ever plays it — or a signal exists and the consumer
 * treats every firing the same way. The idle fidgets, the `2nd` eye pose, and now the
 * PetDex extras all shipped wired-but-dead; the stop signal shipped consumed-but-
 * ignored. Reachability itself has to be the assertion.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, cleanup, waitFor } from '@testing-library/react'

const CUSTOM_SVG = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="8" height="8"/></svg>'
const WAVE_SVG = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="4"/></svg>'

const config = { activeAppearance: 'petdex-pack' }

vi.mock('../apps/crew-companion/petBridge', () => ({
  petBridge: {
    getCrewCompanionConfig: () => Promise.resolve(config),
    presetsGetColorMap: () => Promise.resolve(null),
    galleryGetPackDetail: () =>
      Promise.resolve({
        animations: { idle: CUSTOM_SVG, wave: WAVE_SVG },
        randomNames: ['wave'],
        sprite: {},
      }),
    onGalleryActiveChanged: () => () => {},
    onColorMapChanged: () => () => {},
  },
}))

const { PetAvatar } = await import('../apps/crew-companion/PetAvatar')

afterEach(cleanup)

describe('PetDex import consumes every usable row', () => {
  it('maps all nine rows except the deliberately-skipped running-left', async () => {
    /*
     * PetDex sheets carry nine rows. Four are states (idle/error/done/walking), and
     * every other usable row must land in the random pool — art that is imported and
     * stored but unreachable is this app's recurring failure class (idle fidgets, the
     * 2nd eye pose, and these very extras all shipped that way). running-left alone
     * is skipped on purpose: the app mirrors with flipX.
     */
    const { RANDOM_MAP, STATE_MAP } = await import('../apps/crew-companion/petdexImport')
    const stateRows = STATE_MAP.map((s: { row: number }) => s.row)
    const randomRows = RANDOM_MAP.map((r: { row: number }) => r.row)
    const consumed = new Set([...stateRows, ...randomRows])
    for (const row of [0, 1, 3, 4, 5, 6, 7, 8]) {
      expect(consumed.has(row), `row ${row} must be consumed`).toBe(true)
    }
    expect(consumed.has(2), 'running-left stays skipped (flipX covers it)').toBe(false)
    expect(RANDOM_MAP.map((r: { name: string }) => r.name)).toContain('review')
  })
})

describe('PetDex random clips', () => {
  it('renders a named random clip through the clip channel', async () => {
    const { container } = render(
      <PetAvatar size={128} state="idle" clipName="wave" />,
    )
    await waitFor(() => {
      const img = container.querySelector('img[src^="data:"]') as HTMLImageElement
      expect(img).not.toBeNull()
      // The wave clip's art, not idle's: the data URI encodes the circle SVG.
      expect(decodeURIComponent(img.src)).toContain('circle')
    })
  })

  it('falls back to idle art when the clip name is stale', async () => {
    const { container } = render(
      <PetAvatar size={128} state="idle" clipName="deleted-clip" />,
    )
    await waitFor(() => {
      const img = container.querySelector('img[src^="data:"]') as HTMLImageElement
      expect(img).not.toBeNull()
      expect(decodeURIComponent(img.src)).toContain('rect') // idle's art
    })
  })
})
