/** A settled outcome must not wear the danger surface or interrupt as an alert. */

import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import ErrorNotice from '../components/ErrorNotice'
import { RESOLVED_CLOSE_KINDS } from '../utils/sessionCloseFailure'
import appSource from '../App.tsx?raw'
import artifactSource from '../pages/ArtifactDetailPage.tsx?raw'

describe('a resolved close reports instead of alarming', () => {
  it('renders the success tone without the alert role or danger surface', () => {
    const { container } = render(
      <ErrorNotice message="The session closed as requested." title="“Draft” closed" tone="success" />,
    )
    const box = container.firstElementChild as HTMLElement

    expect(box.getAttribute('role')).toBe('status')
    expect(box.className).toContain('text-ok')
    expect(box.className).not.toContain('text-danger')
  })

  it('still alarms by default, so no existing caller changes tone', () => {
    const { container } = render(<ErrorNotice message="Something broke" />)
    const box = container.firstElementChild as HTMLElement

    expect(box.getAttribute('role')).toBe('alert')
    expect(box.className).toContain('text-danger')
  })

  it('treats confirmed and replaced as the settled kinds', () => {
    expect(RESOLVED_CLOSE_KINDS.has('confirmed')).toBe(true)
    expect(RESOLVED_CLOSE_KINDS.has('replaced')).toBe(true)
    expect(RESOLVED_CLOSE_KINDS.has('unknown')).toBe(false)
    expect(RESOLVED_CLOSE_KINDS.has('refused')).toBe(false)
  })

  it('routes both close surfaces through that set', () => {
    expect(appSource).toMatch(/tone=\{RESOLVED_CLOSE_KINDS\.has\(sessionCloseFailure\.kind\)/)
    expect(artifactSource).toMatch(/RESOLVED_CLOSE_KINDS\.has\(closeFailure\.kind\)/)
  })

  it('drops the agent hand-off on a settled outcome', () => {
    expect(appSource).toMatch(/askAgent=\{!RESOLVED_CLOSE_KINDS\.has\(sessionCloseFailure\.kind\)\}/)
  })
})
