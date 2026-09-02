import { render, screen, fireEvent } from '@testing-library/react'
import SideSheet from './SideSheet'

describe('SideSheet', () => {
  it('renders nothing while closed', () => {
    render(
      <SideSheet open={false} onClose={vi.fn()} label="zzq-sheet" header={<span>zzq-h</span>}>
        <p>zzq-body</p>
      </SideSheet>,
    )
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('renders a labelled modal dialog with header, body and footer', () => {
    render(
      <SideSheet
        open
        onClose={vi.fn()}
        label="zzq-sheet"
        header={<span>zzq-h</span>}
        headerActions={<span>zzq-actions</span>}
        footer={<span>zzq-footer</span>}
        width={480}
      >
        <p>zzq-body</p>
      </SideSheet>,
    )
    const dialog = screen.getByRole('dialog', { name: 'zzq-sheet' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(dialog.style.maxWidth).toBe('480px')
    expect(screen.getByText('zzq-h')).toBeInTheDocument()
    expect(screen.getByText('zzq-actions')).toBeInTheDocument()
    expect(screen.getByText('zzq-body')).toBeInTheDocument()
    expect(screen.getByText('zzq-footer')).toBeInTheDocument()
  })

  it('omits the footer bar when no footer is given', () => {
    render(
      <SideSheet open onClose={vi.fn()} label="zzq-sheet" header={<span>zzq-h</span>}>
        <p>zzq-body</p>
      </SideSheet>,
    )
    expect(screen.queryByText('zzq-footer')).not.toBeInTheDocument()
  })

  it('the backdrop and the X both close, and both are outside the dialog node', () => {
    const onClose = vi.fn()
    const { unmount } = render(
      <SideSheet open onClose={onClose} label="zzq-sheet" header={<span>zzq-h</span>}>
        <p>zzq-body</p>
      </SideSheet>,
    )
    const backdrops = screen.getAllByLabelText('Close panel')
    // One backdrop (Clickable) + one IconButton in the header.
    expect(backdrops.length).toBe(2)
    fireEvent.click(backdrops[0])
    expect(onClose).toHaveBeenCalledTimes(1)
    fireEvent.click(backdrops[1])
    expect(onClose).toHaveBeenCalledTimes(2)
    unmount()
  })

  it('Escape closes while unpaused and is ignored while paused', () => {
    const onClose = vi.fn()
    const { rerender } = render(
      <SideSheet open onClose={onClose} label="zzq-sheet" header={<span>zzq-h</span>} paused>
        <p>zzq-body</p>
      </SideSheet>,
    )
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()

    rerender(
      <SideSheet open onClose={onClose} label="zzq-sheet" header={<span>zzq-h</span>}>
        <p>zzq-body</p>
      </SideSheet>,
    )
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('stops keydown from reaching the page shortcuts underneath', () => {
    const onOuterKeyDown = vi.fn()
    render(
      <div onKeyDown={onOuterKeyDown}>
        <SideSheet open onClose={vi.fn()} label="zzq-sheet" header={<span>zzq-h</span>}>
          <p>zzq-body</p>
        </SideSheet>
      </div>,
    )
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'k' })
    expect(onOuterKeyDown).not.toHaveBeenCalled()
  })
})
