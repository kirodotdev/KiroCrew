// The meeting menu's Export items (#21180).

import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'

import ExportMenuItems, { exportFileName } from './components/ExportMenuItems'
import type { AgentDef } from './api'
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from '../../components/ui/dropdown-menu'

const saveBlob = vi.fn((_blob: Blob, _name: string) => Promise.resolve())
vi.mock('../../utils/download', () => ({
  saveBlob: (blob: Blob, name: string) => saveBlob(blob, name),
}))

const NOTES: AgentDef = { id: 'note-taker', name: 'Note Taker', widget_type: 'markdown' }
const SKETCH: AgentDef = { id: 'sketch-artist', name: 'Sketch Artist', widget_type: 'html' }
// Local noon, so the file's date is the same in every time zone the suite runs in.
const STARTED = new Date(2026, 10, 5, 12).toISOString()

function openMenu(props: Partial<React.ComponentProps<typeof ExportMenuItems>> = {}) {
  const onError = vi.fn()
  render(
    <DropdownMenu>
      <DropdownMenuTrigger>More</DropdownMenuTrigger>
      <DropdownMenuContent>
        <ExportMenuItems
          agents={[NOTES, SKETCH]}
          outputs={{}}
          title="Q3: plan/review"
          date={STARTED}
          onError={onError}
          {...props}
        />
      </DropdownMenuContent>
    </DropdownMenu>,
  )
  fireEvent.keyDown(screen.getByText('More'), { key: 'Enter' })
  return { onError }
}

const item = () => screen.getByText('Export Note Taker output as Markdown').closest('[role="menuitem"]')!

afterEach(() => {
  cleanup()
  saveBlob.mockClear()
})

describe('ExportMenuItems', () => {
  it('saves what the notes panel shows under the meeting title and date, and offers no diagram export', async () => {
    openMenu({ outputs: { 'note-taker': '# Mine\n\nedited by the user', 'sketch-artist': '<p>diagram</p>' } })
    expect(screen.getAllByRole('menuitem')).toHaveLength(1)
    fireEvent.click(item())
    await waitFor(() => expect(saveBlob).toHaveBeenCalledTimes(1))
    const [blob, name] = saveBlob.mock.calls[0]
    expect(name).toBe('Q3- plan-review-2026-11-05-note-taker.md')
    expect(await blob.text()).toBe('# Mine\n\nedited by the user')
  })

  it('offers the export disabled while there is no text to save', async () => {
    openMenu()
    expect(item()).toHaveAttribute('data-disabled')
    fireEvent.click(item())
    expect(saveBlob).not.toHaveBeenCalled()
  })

  it('reports a download the browser could not start', async () => {
    saveBlob.mockImplementationOnce(() => Promise.reject(new Error('hand-off failed')))
    const { onError } = openMenu({ outputs: { 'note-taker': '# Notes' } })
    fireEvent.click(item())
    await waitFor(() => expect(onError).toHaveBeenLastCalledWith('Download failed'))
    // The next attempt clears the previous failure before it starts.
    expect(onError).toHaveBeenNthCalledWith(1, null)
  })
})

describe('exportFileName', () => {
  it('drops the parts a meeting does not have', () => {
    expect(exportFileName('Standup', undefined, 'note-taker')).toBe('Standup-note-taker.md')
    expect(exportFileName('', STARTED, 'note-taker')).toBe('2026-11-05-note-taker.md')
  })

  it('keeps only characters the desktop app can save under the announced name', () => {
    // `~`, a zero-width joiner and a dot (`con.` is a reserved name) are all
    // rewritten by Chromium, so the name would no longer match the announced one.
    expect(exportFileName('.Sync ~ R&D 👩\u200d💻 週次', undefined, 'note-taker'))
      .toBe('Sync - R&D - 週次-note-taker.md')
    expect(exportFileName('con.review', undefined, 'n')).toBe('con-review-n.md')
    expect(exportFileName('会'.repeat(100), undefined, 'n')).toBe(`${'会'.repeat(50)}-n.md`)
  })
})
