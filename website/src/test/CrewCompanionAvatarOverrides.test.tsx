import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, waitFor } from '@testing-library/react'

const IDLE = '<svg xmlns="http://www.w3.org/2000/svg"><rect id="idle"/></svg>'
const DONE = '<svg xmlns="http://www.w3.org/2000/svg"><circle id="done"/></svg>'
const ERROR = '<svg xmlns="http://www.w3.org/2000/svg"><path id="error"/></svg>'
const INHALE = '<svg xmlns="http://www.w3.org/2000/svg"><ellipse id="inhale"/></svg>'

let animations: Record<string, string> = { idle: IDLE }

vi.mock('../apps/crew-companion/petBridge', () => ({
  petBridge: {
    getCrewCompanionConfig: () => Promise.resolve({ activeAppearance: 'custom-pack' }),
    presetsGetColorMap: () => Promise.resolve(null),
    galleryGetPackDetail: () => Promise.resolve({ animations, randomNames: [] }),
    onGalleryActiveChanged: () => () => {},
    onColorMapChanged: () => () => {},
  },
}))

const { PetAvatar } = await import('../apps/crew-companion/PetAvatar')

function decodedArt(container: HTMLElement): string {
  const img = container.querySelector('img[src^="data:"]') as HTMLImageElement | null
  return img ? decodeURIComponent(img.src) : ''
}

beforeEach(() => { animations = { idle: IDLE } })
afterEach(cleanup)

describe('custom state overrides are granular', () => {
  it('keeps Kiro celebration when Success is absent', async () => {
    const { container } = render(<PetAvatar size={128} state="done" />)
    await waitFor(() => expect(decodedArt(container)).toContain('id="idle"'))
    expect(container.querySelector('.kg-anim-celebrate')).not.toBeNull()
  })

  it('uses uploaded Success without layering Kiro celebration', async () => {
    animations.done = DONE
    const { container } = render(<PetAvatar size={128} state="done" />)
    await waitFor(() => expect(decodedArt(container)).toContain('id="done"'))
    expect(container.querySelector('.kg-anim-celebrate')).toBeNull()
  })

  it('keeps Kiro error motion when Failure is absent', async () => {
    const { container } = render(<PetAvatar size={128} state="error" />)
    await waitFor(() => expect(decodedArt(container)).toContain('id="idle"'))
    expect(container.querySelector('.kg-anim-error')).not.toBeNull()
  })

  it('uses uploaded Failure without layering Kiro error motion', async () => {
    animations.error = ERROR
    const { container } = render(<PetAvatar size={128} state="error" />)
    await waitFor(() => expect(decodedArt(container)).toContain('id="error"'))
    expect(container.querySelector('.kg-anim-error')).toBeNull()
  })

  it('reports an uploaded breathing phase as its own replacement', async () => {
    animations.inhale = INHALE
    const onOverride = vi.fn()
    const { container } = render(
      <PetAvatar size={128} state="inhale" onCustomOverrideChange={onOverride} />,
    )
    await waitFor(() => expect(decodedArt(container)).toContain('id="inhale"'))
    await waitFor(() => expect(onOverride).toHaveBeenLastCalledWith(true))
  })
})
