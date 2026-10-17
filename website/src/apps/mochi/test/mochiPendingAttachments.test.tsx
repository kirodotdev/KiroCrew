/**
 * Pending-attachment strip.
 *
 * The strip has no upstream to diff against (it belongs to the kept fork
 * feature), so these are its only pins. Both failures below are silent: a chip
 * with no working remove button leaves the user unable to un-attach a file, and
 * a thumbnail pointed at a file:// URL simply renders blank in a page.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

vi.mock('../panel/panelBridge', () => ({
  localFileUrl: (p: string) => `/api/file-raw?path=${encodeURIComponent(p)}`,
}))

import { PendingAttachments } from '../panel/PendingAttachments'
import type { PendingAttachment } from '../panel/composerDrop'

const img = (path: string): PendingAttachment => ({
  path, name: path.split('/').pop() as string, isImage: true,
})
const file = (path: string): PendingAttachment => ({
  path, name: path.split('/').pop() as string, isImage: false,
})

describe('PendingAttachments', () => {
  it('renders nothing when nothing is attached', () => {
    const { container } = render(<PendingAttachments items={[]} onRemove={() => {}} />)
    expect(container.firstChild).toBeNull()
  })

  it('shows one thumbnail per image, served through core file-raw', () => {
    render(
      <PendingAttachments items={[img('/u/a.png'), img('/u/b.png')]} onRemove={() => {}} />,
    )
    const imgs = screen.getAllByRole('img')
    // Many images, not one — the single-slot limit the fork had is gone.
    expect(imgs).toHaveLength(2)
    // A file:// src would render blank in a page; the gateway route is required.
    expect(imgs[0].getAttribute('src')).toContain('/api/file-raw?path=')
  })

  it('labels a non-image attachment by basename', () => {
    render(<PendingAttachments items={[file('/u/deep/notes.txt')]} onRemove={() => {}} />)
    expect(screen.getByText('notes.txt')).toBeTruthy()
  })

  it('reports the dismissed path so the composer can drop it from state', () => {
    const onRemove = vi.fn()
    render(<PendingAttachments items={[img('/u/a.png'), img('/u/b.png')]} onRemove={onRemove} />)
    fireEvent.click(screen.getByLabelText('Remove: b.png'))
    expect(onRemove).toHaveBeenCalledWith('/u/b.png')
  })

  it('gives every chip its own accessible label', () => {
    // A 40px thumbnail is not identifiable on its own; the label carries the name.
    render(<PendingAttachments items={[img('/u/a.png'), file('/u/n.txt')]} onRemove={() => {}} />)
    expect(screen.getByLabelText('Remove: a.png')).toBeTruthy()
    expect(screen.getByLabelText('Remove: n.txt')).toBeTruthy()
  })
})
