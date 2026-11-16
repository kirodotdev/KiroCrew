import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { renderUserContent } from '../pages/chat/ChatPageMessageContent'

// An `/artifacts/<slug>` link in the user's OWN message opens the side
// Artifacts panel, through the same handler assistant and note rows get.
describe('renderUserContent — artifact links', () => {
  const content = 'See [Release checklist](/artifacts/release-checklist)'

  it('a plain click opens the panel and does not navigate', () => {
    const onArtifactOpen = vi.fn()
    const { getByRole } = render(<>{renderUserContent({ content, onArtifactOpen })}</>)
    const link = getByRole('link', { name: 'Release checklist' })
    // fireEvent returns false when a handler called preventDefault.
    expect(fireEvent.click(link)).toBe(false)
    expect(onArtifactOpen).toHaveBeenCalledWith('release-checklist')
  })

  it.each([['Cmd', { metaKey: true }], ['Ctrl', { ctrlKey: true }]])('a %s-click keeps the browser new tab', (_k, mod) => {
    const onArtifactOpen = vi.fn()
    const { getByRole } = render(<>{renderUserContent({ content, onArtifactOpen })}</>)
    expect(fireEvent.click(getByRole('link', { name: 'Release checklist' }), mod)).toBe(true)
    expect(onArtifactOpen).not.toHaveBeenCalled()
  })

  it('a caption beside a file card opens the panel too', () => {
    const onArtifactOpen = vi.fn()
    const { getByRole } = render(
      <>{renderUserContent({ content, meta: { files: ['/repo/notes.txt'] }, onFileOpen: () => {}, onArtifactOpen })}</>,
    )
    expect(fireEvent.click(getByRole('link', { name: 'Release checklist' }))).toBe(false)
    expect(onArtifactOpen).toHaveBeenCalledWith('release-checklist')
  })
})
