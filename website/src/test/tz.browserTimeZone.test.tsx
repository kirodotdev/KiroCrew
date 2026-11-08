/** A browser that cannot name its own zone must not crash the Schedule UI.
 *
 *  On Windows, a new Windows-only zone ID the bundled ICU cannot map makes
 *  `Intl.DateTimeFormat().resolvedOptions().timeZone` undefined. The new-job
 *  form then called `.replace` on it and the page unmounted. These tests force
 *  that undefined and check the UI falls back to UTC instead.
 */

import { render, screen } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import JobForm from '../components/JobForm'
import WeekGrid from '../components/WeekGrid'
import { browserTimeZone, isValidTimeZone } from '../utils/tz'

vi.mock('../api/client', () => ({
  api: {
    updateCron: vi.fn(),
    createCron: vi.fn(),
    models: vi.fn().mockResolvedValue({ models: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    agentCatalog: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
  },
}))

function forceMissingBrowserZone() {
  const real = Intl.DateTimeFormat.prototype.resolvedOptions
  return vi.spyOn(Intl.DateTimeFormat.prototype, 'resolvedOptions').mockImplementation(function (this: Intl.DateTimeFormat) {
    return { ...real.call(this), timeZone: undefined as unknown as string }
  })
}

afterEach(() => { vi.restoreAllMocks() })

describe('browserTimeZone', () => {
  it('returns the browser zone when Intl names one', () => {
    vi.spyOn(Intl.DateTimeFormat.prototype, 'resolvedOptions').mockReturnValue({ timeZone: 'America/Vancouver' } as Intl.ResolvedDateTimeFormatOptions)
    expect(browserTimeZone()).toBe('America/Vancouver')
  })

  it('falls back to UTC when Intl reports no zone', () => {
    forceMissingBrowserZone()
    expect(browserTimeZone()).toBe('UTC')
  })

  it('falls back to UTC when Intl reports a zone it cannot format', () => {
    vi.spyOn(Intl.DateTimeFormat.prototype, 'resolvedOptions').mockReturnValue({ timeZone: 'British Columbia Standard Time' } as Intl.ResolvedDateTimeFormatOptions)
    expect(browserTimeZone()).toBe('UTC')
  })
})

describe('isValidTimeZone', () => {
  it.each([
    ['America/Vancouver', true],
    ['UTC', true],
    ['undefined', false],
    ['', false],
    [null, false],
    [undefined, false],
  ])('%s -> %s', (tz, ok) => {
    expect(isValidTimeZone(tz)).toBe(ok)
  })
})

describe('Schedule UI without a browser zone', () => {
  it('opens the new-job form', async () => {
    forceMissingBrowserZone()
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    expect(await screen.findByLabelText('Name')).toBeInTheDocument()
  })

  it('renders the week grid', () => {
    forceMissingBrowserZone()
    expect(() => render(<WeekGrid jobs={[]} selectedId={null} onSelect={() => {}} />)).not.toThrow()
  })
})
