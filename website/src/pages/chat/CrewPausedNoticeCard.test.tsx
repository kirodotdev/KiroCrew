import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'

import CrewPausedNoticeCard from './CrewPausedNoticeCard'
import { turnOffCrewModeAsLts } from '../../test/crewModeOff'

describe('CrewPausedNoticeCard', () => {
  it('renders nothing in a build without conductor mode (LTS)', () => {
    turnOffCrewModeAsLts()
    const { container } = render(<CrewPausedNoticeCard stopped={2} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('says how many crew members the pause stopped', () => {
    render(<CrewPausedNoticeCard stopped={2} />)
    expect(screen.getByTestId('crew-paused-notice-card')).toBeInTheDocument()
    const text = screen.getByTestId('crew-paused-notice-text').textContent ?? ''
    expect(text).toMatch(/2/)
    expect(text).not.toMatch(/conductor_mode_paused_notice/)
  })
})
