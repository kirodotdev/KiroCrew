import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import OutlineView from '../apps/pptx-maker/OutlineView'

const OUTLINE = `# Coffee brewing basics

## Methods

- [pour-over] Pour-over gives the cleanest cup
  - body: slow pour through a paper filter
  - visual: a three-step flow
  - evidence: brief Sources
- [french-press] French press is the most forgiving
  - body: [TBD]
`

describe('OutlineView', () => {
  it('renders one card per slide with its claim, body, visual and evidence', () => {
    render(<OutlineView markdown={OUTLINE} />)
    expect(screen.getByText('Coffee brewing basics')).toBeTruthy()
    expect(screen.getAllByTestId('outline-slide')).toHaveLength(2)
    expect(screen.getByText('Pour-over gives the cleanest cup')).toBeTruthy()
    expect(screen.getByText('slow pour through a paper filter')).toBeTruthy()
    expect(screen.getByText('a three-step flow')).toBeTruthy()
    expect(screen.getByText('brief Sources')).toBeTruthy()
    expect(screen.getByText('Methods')).toBeTruthy()
  })

  it('flags a card that still carries a [TBD]', () => {
    render(<OutlineView markdown={OUTLINE} />)
    const [done, pending] = screen.getAllByTestId('outline-slide')
    expect(done.className).not.toContain('border-warn')
    expect(pending.className).toContain('border-warn')
  })

  it('switches to the raw Markdown on request', async () => {
    render(<OutlineView markdown={OUTLINE} />)
    await userEvent.click(screen.getByText('Markdown'))
    expect(screen.queryAllByTestId('outline-slide')).toHaveLength(0)
  })

  it('falls back to Markdown for an outline outside the engine grammar', () => {
    render(<OutlineView markdown={'Just some notes\n\n- a bullet'} />)
    expect(screen.queryAllByTestId('outline-slide')).toHaveLength(0)
    expect(screen.queryByText('Storyboard')).toBeNull()
    expect(screen.getByText('Just some notes')).toBeTruthy()
  })
})
