/**
 * Celebrate props render ONLY on the built-in ghost.
 *
 * A prop's placement is derived from GHOST_EYE_MAP -- the built-in ghost's eye
 * geometry. On a custom pack (a different silhouette entirely) a hat or shades land by
 * a face that is not the pack's own, floating in empty space beside it. The eye overlay
 * was already `isDefault`-gated for the same reason; before this fix the accessory layer
 * was not, so a user who picked a custom avatar got a party hat hovering off its head on
 * every session-done celebration.
 *
 * This drives the REAL PetAvatar with a mocked appearance backend: once as the built-in
 * pack (prop present) and once as a custom pack (prop absent), which is the whole point.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, cleanup, waitFor } from '@testing-library/react'

// A tiny inline SVG so the custom pack resolves to a real `svg` art kind.
const CUSTOM_SVG = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>'

const config = { activeAppearance: 'kiro-ghost' as string }

vi.mock('../apps/crew-companion/petBridge', () => ({
  petBridge: {
    getCrewCompanionConfig: () => Promise.resolve(config),
    presetsGetColorMap: () => Promise.resolve(null),
    galleryGetPackDetail: () =>
      Promise.resolve({ animations: { idle: CUSTOM_SVG }, sprite: {} }),
    onGalleryActiveChanged: () => () => {},
    onColorMapChanged: () => () => {},
  },
}))

// Imported AFTER the mock is registered.
const { PetAvatar } = await import('../apps/crew-companion/PetAvatar')

afterEach(cleanup)

describe('celebrate props are built-in only', () => {
  beforeEach(() => {
    config.activeAppearance = 'kiro-ghost'
  })

  it('draws the party hat on the built-in ghost', async () => {
    const { container } = render(
      <PetAvatar size={128} state="done" accessory="partyhat" />,
    )
    await waitFor(() => {
      expect(container.querySelector('img[src*="partyhat"]')).not.toBeNull()
    })
  })

  it('does NOT draw the party hat on a custom pack', async () => {
    config.activeAppearance = 'some-custom-pack-id'
    const { container } = render(
      <PetAvatar size={128} state="done" accessory="partyhat" />,
    )
    // Wait until the custom art has resolved (the body <img> appears), THEN assert the
    // prop is absent -- otherwise the test could pass on the pre-resolve default frame.
    await waitFor(() => {
      expect(container.querySelector('img[src^="data:"]')).not.toBeNull()
    })
    expect(container.querySelector('img[src*="partyhat"]')).toBeNull()
  })
})
