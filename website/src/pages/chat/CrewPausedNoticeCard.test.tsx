import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'

import CrewPausedNoticeCard from './CrewPausedNoticeCard'
import { PREVIEW_CREW_MODE, setPreviewFlag } from '../../utils/previewFlags'

describe('CrewPausedNoticeCard', () => {
  beforeEach(() => setPreviewFlag(PREVIEW_CREW_MODE, true))
  afterEach(() => localStorage.removeItem(PREVIEW_CREW_MODE))

  it('renders nothing while the Ultra Mode preview is OFF', () => {
    setPreviewFlag(PREVIEW_CREW_MODE, false)
    const { container } = render(<CrewPausedNoticeCard stopped={2} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('says how many crew members the pause stopped', () => {
    render(<CrewPausedNoticeCard stopped={2} />)
    expect(screen.getByTestId('crew-paused-notice-card')).toBeInTheDocument()
    const text = screen.getByTestId('crew-paused-notice-text').textContent ?? ''
    expect(text).toMatch(/2/)
    expect(text).not.toMatch(/ultra_paused_notice/)
  })
})
