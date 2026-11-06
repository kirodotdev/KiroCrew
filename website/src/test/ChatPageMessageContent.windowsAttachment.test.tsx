import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { renderUserContent } from '../pages/chat/ChatPageMessageContent'

// Only an attachment card draws the paperclip icon; an inline chip has none.
const cardsIn = (root: HTMLElement) => [...root.querySelectorAll('svg.lucide-paperclip')].map(svg => svg.parentElement as HTMLElement)

// A sent message shows each inline attachment once: as its chip, not also as a card.
describe('renderUserContent with a mixed-separator Windows path', () => {
  it('renders a working inline chip and no extra attachment card', () => {
    const p = 'C:/repo\\src\\a.ts'
    const onFileOpen = vi.fn()
    const { container } = render(<>{renderUserContent({
      content: `see [attached_file 1] ${p} please`,
      meta: { files: [p] },
      onFileOpen,
    })}</>)
    const chip = screen.getByText('@a.ts')
    fireEvent.click(chip)
    expect(onFileOpen).toHaveBeenCalledWith(p)
    expect(cardsIn(container).length).toBe(0)
  })

  it('still renders a card for a listed file the text never references', () => {
    const p = 'C:/repo\\src\\a.ts'
    const other = 'C:\\repo\\notes.md'
    const { container } = render(<>{renderUserContent({
      content: `see [attached_file 1] ${p} please`,
      meta: { files: [p, other] },
      onFileOpen: () => {},
    })}</>)
    const cards = cardsIn(container)
    expect(cards.length).toBe(1)
    expect(cards[0].textContent).toContain('notes.md')
  })

  it('keeps a card for a mention past the inline chip cap', () => {
    const files = Array.from({ length: 21 }, (_, i) => `C:/repo\\src\\f${i + 1}.ts`)
    const content = files.map((p, i) => `see [attached_file ${i + 1}] ${p} please`).join('\n')
    const { container } = render(<>{renderUserContent({ content, meta: { files }, onFileOpen: () => {} })}</>)
    expect(screen.getAllByRole('button', { name: /^Open file C:\/repo\\src\\f\d+\.ts$/ })
      .filter(el => el.textContent?.startsWith('@')).length).toBe(20)
    const cards = cardsIn(container)
    expect(cards.length).toBe(1)
    expect(cards[0].textContent).toContain('f21.ts')
  })

  it('keeps a card for a marker whose label the text does not set off as a chip', () => {
    const p = 'C:/repo\\src\\a.ts'
    const { container } = render(<>{renderUserContent({
      content: `x[attached_file 1] ${p} more`,
      meta: { files: [p] },
      onFileOpen: () => {},
    })}</>)
    const cards = cardsIn(container)
    expect(cards.length).toBe(1)
    expect(cards[0].textContent).toContain('a.ts')
  })
})
