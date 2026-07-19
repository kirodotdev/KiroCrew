import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { useRef } from 'react'
import { Copy, MessageSquarePlus } from 'lucide-react'
import SelectionToolbar, { type SelectionAction } from '../components/SelectionToolbar'

// Defect #5: the ArtifactPanel copy selection action previously used a literal
// text `<span>Copy</span>` as its `icon` PLUS `label: 'Copy'`. SelectionToolbar
// renders BOTH the icon and the label, so the button read "Copy Copy". The fix
// uses a lucide `<Copy />` icon (matching the file panel). This test verifies
// that, given an icon that is a lucide element (not a text node), the rendered
// copy button's text content is exactly "Copy" — i.e. no doubling.

function Harness({ actions }: { actions: SelectionAction[] }) {
  const ref = useRef<HTMLDivElement>(null)
  return (
    <div ref={ref}>
      {/* externalSelection forces the toolbar visible without a real DOM range */}
      <SelectionToolbar containerRef={ref} actions={actions} externalSelection={{ text: 'hello', x: 10, y: 10 }} />
    </div>
  )
}

describe('SelectionToolbar copy action (ArtifactPanel defect #5)', () => {
  it('renders the copy action label exactly once when the icon is a lucide element', () => {
    const actions: SelectionAction[] = [
      { id: 'comment', icon: <MessageSquarePlus size={12} />, label: 'Comment', onClick: vi.fn() },
      { id: 'copy', icon: <Copy size={12} />, label: 'Copy', onClick: vi.fn() },
    ]
    render(<Harness actions={actions} />)
    const copyBtn = screen.getByRole('button', { name: 'Copy' })
    // The bug rendered "CopyCopy" (icon-as-text-span + label). With a lucide
    // icon (an <svg>), the button's textContent is just the label.
    expect(copyBtn.textContent).toBe('Copy')
    expect(copyBtn.querySelector('svg')).toBeTruthy()
  })

  it('regression guard: a text-span icon WOULD double the label (documents the old bug)', () => {
    const actions: SelectionAction[] = [
      { id: 'copy', icon: <span>Copy</span>, label: 'Copy', onClick: vi.fn() },
    ]
    render(<Harness actions={actions} />)
    const copyBtn = screen.getByRole('button', { name: /Copy/ })
    // This is the old, broken shape — it doubles. Asserting it here pins the
    // root cause so the fix's intent is unambiguous.
    expect(copyBtn.textContent).toBe('CopyCopy')
  })

  it('fires the copy onClick with the selected text', () => {
    const onCopy = vi.fn()
    const actions: SelectionAction[] = [
      { id: 'copy', icon: <Copy size={12} />, label: 'Copy', onClick: onCopy },
    ]
    render(<Harness actions={actions} />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    expect(onCopy).toHaveBeenCalledWith('hello', expect.anything())
  })
})
