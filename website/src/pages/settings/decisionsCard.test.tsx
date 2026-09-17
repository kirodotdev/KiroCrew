/**
 * The Decisions (Jev) card in Settings > Developer > Feature Previews.
 *
 * What is under test is the one thing this card does differently from every
 * other preview in that section: its switch is a `config.json` value written
 * through `PATCH /api/config/kirocrew`, not a per-device localStorage flag. So
 * the cases are the states a backend-backed switch can be in — read pending,
 * read failed, section absent, section present, write failed — and the claim in
 * each is the same one: the card never offers a write it cannot make, and never
 * stays silent about why.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { api } from '../../api/client'
import { PREVIEW_FLAG_PREFIX } from '../../utils/previewFlags'
import { FeaturePreviewsSection } from './FeaturePreviewsSection'

/** Rendered through the whole section, because that is where the card ships. */
function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter><FeaturePreviewsSection /></MemoryRouter>
    </QueryClientProvider>,
  )
}

/** The card's own switch. Named in full so "Decisions" cannot match another row. */
const decisionsSwitch = () => screen.getByRole('switch', { name: 'Decisions (Jev)' })

describe('Decisions (Jev) preview card', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('offers no write while the config has not been read', () => {
    // A never-resolving read: the switch has no basis for the state it would
    // show, so it must not be clickable in the meantime.
    vi.spyOn(api, 'kirocrewConfig').mockReturnValue(new Promise(() => {}) as never)
    renderSection()
    expect(decisionsSwitch().getAttribute('aria-disabled')).toBe('true')
  })

  it('disables itself and names the gateway when the config carries no decisions section', async () => {
    // The state on `main` today: the frontend ships before the backend field.
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ telemetry: {} } as never)
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/older than this switch/i)).toBeInTheDocument()
    })
    expect(decisionsSwitch().getAttribute('aria-disabled')).toBe('true')
    expect(decisionsSwitch().getAttribute('aria-checked')).toBe('false')
  })

  it('says the read failed rather than blaming the gateway version', async () => {
    // Two different facts, two different fixes: an old gateway needs an update,
    // a failed read needs a retry. Neither may be reported as the other.
    vi.spyOn(api, 'kirocrewConfig').mockRejectedValue(new Error('offline'))
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/could not read the settings/i)).toBeInTheDocument()
    })
    expect(screen.queryByText(/older than this switch/i)).toBeNull()
    expect(decisionsSwitch().getAttribute('aria-disabled')).toBe('true')
  })

  it('reflects the stored flag and writes the config path when flipped', async () => {
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ decisions: { preview: false } } as never)
    const patch = vi.spyOn(api, 'patchConfig').mockResolvedValue({} as never)
    renderSection()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    })
    decisionsSwitch().click()
    await waitFor(() => {
      // The path matters more than the click: a localStorage key here would look
      // identical on screen and be invisible to the gate that reads it.
      expect(patch).toHaveBeenCalledWith('decisions.preview', true)
    })
  })

  it('shows the stored flag as on, and offers turning it back off', async () => {
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ decisions: { preview: true } } as never)
    const patch = vi.spyOn(api, 'patchConfig').mockResolvedValue({} as never)
    renderSection()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-checked')).toBe('true')
    })
    decisionsSwitch().click()
    await waitFor(() => {
      expect(patch).toHaveBeenCalledWith('decisions.preview', false)
    })
  })

  it('stays closed to input until the write is reflected in a fresh read', async () => {
    // The window this closes: react-query holds a mutation pending only while
    // `onSettled` has an unresolved promise outstanding. Started-but-not-returned,
    // the switch came back to life the instant the PATCH resolved and still showed
    // the pre-flip value — so the flip read as having failed, and a second click
    // wrote it again. The second read is held open here to sit inside that window.
    let releaseRefetch: (value: unknown) => void = () => {}
    let reads = 0
    vi.spyOn(api, 'kirocrewConfig').mockImplementation((() => {
      reads += 1
      if (reads === 1) return Promise.resolve({ decisions: { preview: false } })
      return new Promise(resolve => { releaseRefetch = resolve })
    }) as never)
    vi.spyOn(api, 'patchConfig').mockResolvedValue({} as never)

    renderSection()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    })
    decisionsSwitch().click()
    await waitFor(() => {
      expect(api.patchConfig).toHaveBeenCalledWith('decisions.preview', true)
    })
    // The PATCH has resolved and the refetch has not. The switch still shows the
    // stored value, so it must not accept another click against it.
    expect(decisionsSwitch().getAttribute('aria-checked')).toBe('false')
    expect(decisionsSwitch().getAttribute('aria-disabled')).toBe('true')

    releaseRefetch({ decisions: { preview: true } })
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-checked')).toBe('true')
    })
    expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
  })

  it('reports a refused write instead of leaving the switch looking flipped', async () => {
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ decisions: { preview: false } } as never)
    vi.spyOn(api, 'patchConfig').mockRejectedValue(new Error('field not editable'))
    renderSection()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    })
    decisionsSwitch().click()
    await waitFor(() => {
      expect(screen.getByText(/could not save this setting/i)).toBeInTheDocument()
    })
    // The switch shows the config's value, not the click's, so a refused write
    // cannot leave the card claiming the preview is on.
    expect(decisionsSwitch().getAttribute('aria-checked')).toBe('false')
  })

  it('states that the message text leaves the machine, whatever else it says', async () => {
    // The egress sentence is the consent this card asks for. It renders in every
    // state — including the disabled ones — because a reader who cannot flip the
    // switch yet is still deciding whether they ever will.
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ telemetry: {} } as never)
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/leave this machine.*sent over the internet/i)).toBeInTheDocument()
    })
  })

  it('lists the arms the config exposes, and nothing when it exposes none', async () => {
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({
      decisions: {
        preview: true,
        points: { 'skills.select': { arm: 'shadow' }, 'cron.novelty': { arm: 'off' } },
      },
    } as never)
    renderSection()
    await waitFor(() => {
      expect(screen.getByText('skills.select')).toBeInTheDocument()
    })
    expect(screen.getByText('cron.novelty')).toBeInTheDocument()
    // The point with no arm in the config gets no row: there is nothing true to
    // print in it.
    expect(screen.queryByText('skills.dedupe')).toBeNull()
  })

  it('carries no point rows at all when the config has no arms to show', async () => {
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ decisions: { preview: false } } as never)
    renderSection()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    })
    expect(screen.queryByText(/each check below shows the mode/i)).toBeNull()
  })

  it('leaves the four localStorage previews alone', async () => {
    // The section mixes two kinds of switch now. Flipping the backend one must
    // not write a preview flag — a stray one would turn an unrelated unreleased
    // page on for this device.
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ decisions: { preview: false } } as never)
    vi.spyOn(api, 'patchConfig').mockResolvedValue({} as never)
    renderSection()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    })
    decisionsSwitch().click()
    await waitFor(() => {
      expect(api.patchConfig).toHaveBeenCalled()
    })
    expect(Object.keys(localStorage).filter(k => k.startsWith(PREVIEW_FLAG_PREFIX))).toEqual([])
  })
})
