/**
 * The pet's LIVE appearance switch must hand the renderer the same shape the
 * mount path does.
 *
 * The bug this pins: `onGalleryActiveChanged` built its payload from the RAW
 * manifest, whose `states`/`moods` values are FILENAMES and which has no
 * `animations` key. PetWidget's handler is written
 * `if (data?.meta && data?.animations)`, so every live switch to a user pack was
 * a silent no-op — the pet kept whatever art it had (the compiled-in orange cat
 * whenever no resolver was set) and only a RESTART showed the chosen pack,
 * because the mount path goes through the flattening builder in the seam.
 *
 * "Apply did nothing / the pet fell back to the cat" is therefore a payload
 * assertion, which is what this file makes.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const galleryGetPackDetail = vi.fn()
let settingsListener: ((payload: unknown) => void) | undefined

vi.mock('../panel/panelBridge', () => ({
  subscribeAppEvent: () => () => {},
  onColorMapChanged: (cb: (payload: unknown) => void) => {
    settingsListener = cb
    return () => {}
  },
  getPetState: vi.fn(),
  onStateChange: () => () => {},
  onMood: () => () => {},
  onGalleryPacksChanged: () => () => {},
  onWatchlistChanged: () => () => {},
  onNotification: () => () => {},
  galleryGetPackDetail: (id: string) => galleryGetPackDetail(id),
  galleryPackFileUrl: (packId: string, filename: string) =>
    `/api/apps/mochi/packs/${packId}/file/${filename}`,
  disableApp: vi.fn(),
  getWatchlist: vi.fn(),
  getPinnedFiles: vi.fn(),
  markPinnedSeen: vi.fn(),
  unpinFile: vi.fn(),
  setWatchItemStatus: vi.fn(),
  updateWatchItem: vi.fn(),
  reportStat: vi.fn(),
}))

let bridge: typeof import('../pet/petBridge')

beforeEach(async () => {
  vi.resetModules()
  settingsListener = undefined
  galleryGetPackDetail.mockReset()
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: true, text: async () => '<svg id="pack-art" />' })),
  )
  bridge = await import('../pet/petBridge')
})

/**
 * Drive one settings broadcast and let the async emit settle.
 *
 * Real timers on purpose: the subscription also arms a 5s backstop poll, and
 * running fake timers to exhaustion spins that forever. The unsubscribe at the
 * end disarms it so the interval cannot outlive the test.
 */
async function applyAppearance(packId: string): Promise<Record<string, unknown>[]> {
  const seen: Record<string, unknown>[] = []
  const off = bridge.onGalleryActiveChanged((data) => seen.push(data))
  settingsListener?.({ activeAppearance: packId })
  for (let i = 0; i < 20 && seen.length === 0; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0))
  }
  off()
  return seen
}

describe('live appearance switch payload', () => {
  it('inlines a user pack\u2019s art, not its filenames', async () => {
    galleryGetPackDetail.mockResolvedValue({
      meta: { id: 'p1', format: 'svg' },
      states: { idle: 'idle.svg' },
      moods: {},
    })

    const [data] = await applyAppearance('p1')

    expect(data?.packId).toBe('p1')
    // Without these two the renderer's guard is false and the switch is a no-op.
    expect(data?.meta).toBeDefined()
    const animations = data?.animations as Record<string, { content: string; format: string }>
    expect(animations?.idle?.content).toContain('pack-art')
    expect(animations?.idle?.format).toBe('svg')
  })

  it('produces a built-in\u2019s art locally (the packs route would 404)', async () => {
    const [data] = await applyAppearance('kiro-ghost')

    expect(galleryGetPackDetail).not.toHaveBeenCalled()
    expect(data?.packId).toBe('kiro-ghost')
    expect(Object.keys(data?.animations as object)).toContain('idle')
  })

  it('names the pack when it has no readable detail', async () => {
    galleryGetPackDetail.mockResolvedValue(null)
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})

    const [data] = await applyAppearance('gone')

    // Still emitted (the id changed), but the failure is no longer silent.
    expect(data?.packId).toBe('gone')
    expect(data?.animations).toBeUndefined()
    expect(err).toHaveBeenCalledWith(expect.stringContaining('no readable detail'), 'gone')
    err.mockRestore()
  })

  it('reports the cat by id alone \u2014 the pet holds its art compiled in', async () => {
    const [data] = await applyAppearance('default-mochi')

    expect(data).toEqual({ packId: 'default-mochi' })
  })
})
