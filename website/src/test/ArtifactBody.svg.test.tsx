import { useState } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ArtifactBodyNative } from '../components/ArtifactBody'

vi.mock('@monaco-editor/react', () => ({
  default: ({ value, onChange }: { value: string; onChange?: (value: string) => void }) => (
    <textarea
      aria-label="SVG source editor"
      value={value}
      onChange={event => onChange?.(event.target.value)}
    />
  ),
}))

vi.mock('../utils/monacoLocal', () => ({
  ensureMonacoLocal: vi.fn().mockResolvedValue(undefined),
}))

// Fixtures size themselves with width/height instead of a view box: the repo's
// no-inline-icon-SVG rule keys on that attribute, and nothing here needs a
// viewport transform — the assertions only look for the shapes the sanitizer kept.
const INITIAL_SVG = '<svg width="20" height="20"><circle data-testid="old-shape" cx="10" cy="10" r="4"/></svg>'
const UPDATED_SVG = '<svg width="20" height="20"><rect data-testid="new-shape" width="20" height="20"/></svg>'

function SvgEditHarness() {
  const [content, setContent] = useState(INITIAL_SVG)
  return (
    <ArtifactBodyNative
      kind="svg"
      content={content}
      editing
      onChange={setContent}
      previewRef={{ current: null }}
    />
  )
}

describe('ArtifactBodyNative SVG editing', () => {
  it('shows a sanitized live preview and source editor together', async () => {
    render(<SvgEditHarness />)

    expect(screen.getByText('Editing SVG')).toBeInTheDocument()
    expect(screen.getByText('Changes update the preview as you type.')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Preview' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'SVG source code' })).toBeInTheDocument()
    expect(document.querySelector('[data-testid="old-shape"]')).not.toBeNull()

    fireEvent.change(await screen.findByRole('textbox', { name: 'SVG source editor' }), {
      target: { value: UPDATED_SVG },
    })

    expect(document.querySelector('[data-testid="old-shape"]')).toBeNull()
    expect(document.querySelector('[data-testid="new-shape"]')).not.toBeNull()
  })

  it('keeps read mode as a preview-only surface', () => {
    render(
      <ArtifactBodyNative
        kind="svg"
        content={INITIAL_SVG}
        editing={false}
        onChange={vi.fn()}
        previewRef={{ current: null }}
      />,
    )

    expect(document.querySelector('[data-testid="old-shape"]')).not.toBeNull()
    expect(screen.queryByRole('region', { name: 'SVG source code' })).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })
})
