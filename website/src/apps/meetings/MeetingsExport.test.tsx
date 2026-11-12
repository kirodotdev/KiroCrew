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
const sketchItem = () => screen.getByText('Export Sketch Artist output as HTML').closest('[role="menuitem"]')!

afterEach(() => {
  cleanup()
  saveBlob.mockClear()
  vi.unstubAllGlobals()
})

describe('ExportMenuItems', () => {
  it('saves what the notes panel shows under the meeting title and date', async () => {
    openMenu({ outputs: { 'note-taker': '# Mine\n\nedited by the user', 'sketch-artist': '<p>diagram</p>' } })
    // Two single-output items plus the one "Export all outputs" item (#32982).
    expect(screen.getAllByRole('menuitem')).toHaveLength(3)
    fireEvent.click(item())
    await waitFor(() => expect(saveBlob).toHaveBeenCalledTimes(1))
    const [blob, name] = saveBlob.mock.calls[0]
    expect(name).toBe('Q3- plan-review-2026-11-05-note-taker.md')
    expect(await blob.text()).toBe('# Mine\n\nedited by the user')
  })

  it('saves the sketch as one HTML file carrying the vendored runtime and the frame policy', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(new Response('window.mermaid = {}')))
    vi.stubGlobal('fetch', fetchMock)
    openMenu({ outputs: { 'sketch-artist': '<div class="mermaid">graph TD; A-->B</div><script>steal()</script>' } })
    fireEvent.click(sketchItem())
    await waitFor(() => expect(saveBlob).toHaveBeenCalledTimes(1))
    const [blob, name] = saveBlob.mock.calls[0]
    expect(name).toBe('Q3- plan-review-2026-11-05-sketch-artist.html')
    expect(fetchMock).toHaveBeenCalledWith('/vendor/mermaid.min.js')
    const file = await blob.text()
    expect(file).toContain("script-src 'unsafe-inline'; ")
    expect(file).toContain('window.mermaid = {}')
    expect(file).toContain('graph TD; A--&gt;B')
    expect(file).not.toContain('steal()')
  })

  it('offers each export disabled while its agent has no output', async () => {
    openMenu()
    expect(item()).toHaveAttribute('data-disabled')
    expect(sketchItem()).toHaveAttribute('data-disabled')
    fireEvent.click(item())
    fireEvent.click(sketchItem())
    expect(saveBlob).not.toHaveBeenCalled()
  })

  it('reports a sketch export whose runtime could not be read', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response('', { status: 404 }))))
    const { onError } = openMenu({ outputs: { 'sketch-artist': '<p>diagram</p>' } })
    fireEvent.click(sketchItem())
    await waitFor(() => expect(onError).toHaveBeenLastCalledWith('Download failed'))
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
    expect(exportFileName('Standup', undefined, 'sketch-artist', 'html')).toBe('Standup-sketch-artist.html')
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

const allItem = () => screen.getByText('Export all outputs').closest('[role="menuitem"]')!

describe('ExportMenuItems export-all', () => {
  it('saves one HTML file named after the meeting, with every enabled output', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response('window.mermaid = {}'))))
    openMenu({
      outputs: { 'note-taker': '# Mine\n\nedited', 'sketch-artist': '<div class="mermaid">graph TD; A-->B</div>' },
    })
    fireEvent.click(allItem())
    await waitFor(() => expect(saveBlob).toHaveBeenCalledTimes(1))
    const [blob, name] = saveBlob.mock.calls[0]
    expect(name).toBe('Q3- plan-review-2026-11-05.html')
    expect(blob.type).toBe('text/html;charset=utf-8')
    const file = await blob.text()
    // Both agents are present, each under its own heading.
    expect(file).toContain('<h2>Note Taker</h2>')
    expect(file).toContain('<h2>Sketch Artist</h2>')
    // The meeting title and date head the bundle.
    expect(file).toContain('<h1>Q3: plan/review</h1>')
    expect(file).toContain('2026-11-05')
  })

  it('puts notes in as escaped Markdown text, never as raw HTML', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response('window.mermaid = {}'))))
    openMenu({ outputs: { 'note-taker': '# Notes <img src=x onerror=alert(1)>' } })
    fireEvent.click(allItem())
    await waitFor(() => expect(saveBlob).toHaveBeenCalledTimes(1))
    const file = await saveBlob.mock.calls[0][0].text()
    // The note's angle brackets are escaped inside a <pre>, so the <img> is text.
    expect(file).toContain('# Notes &lt;img src=x onerror=alert(1)&gt;')
    expect(file).not.toContain('<img src=x onerror=alert(1)>')
  })

  it('keeps a sketch in its own sandboxed frame, not inlined into the bundle page', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response('window.mermaid = {}'))))
    openMenu({ outputs: { 'sketch-artist': '<div class="mermaid">graph TD; A-->B</div><script>steal()</script>' } })
    fireEvent.click(allItem())
    await waitFor(() => expect(saveBlob).toHaveBeenCalledTimes(1))
    const file = await saveBlob.mock.calls[0][0].text()
    // The sketch rides in a sandboxed iframe srcdoc, so its document (and CSP)
    // is escaped into the attribute rather than merged into the bundle DOM.
    expect(file).toContain('<iframe class="sketch-frame" sandbox="allow-scripts" srcdoc="')
    // The sketch's own CSP survives, escaped, inside the srcdoc.
    expect(file).toContain('Content-Security-Policy')
    // The model's script was scrubbed by buildSketchExport; it is never inlined
    // as a live <script> on the bundle page.
    expect(file).not.toContain('<script>steal()</script>')
    expect(file).not.toContain('steal()')
  })

  it('lists an enabled agent with no output as "No output"', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response('window.mermaid = {}'))))
    openMenu({ outputs: { 'note-taker': '# Mine' } })
    fireEvent.click(allItem())
    await waitFor(() => expect(saveBlob).toHaveBeenCalledTimes(1))
    const file = await saveBlob.mock.calls[0][0].text()
    expect(file).toContain('<h2>Sketch Artist</h2><p class="no-output">No output</p>')
  })

  it('is disabled only while no agent has any output', () => {
    openMenu()
    expect(allItem()).toHaveAttribute('data-disabled')
    cleanup()
    openMenu({ outputs: { 'note-taker': '# Mine' } })
    expect(allItem()).not.toHaveAttribute('data-disabled')
  })

  it('reports a bundle whose runtime could not be read', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response('', { status: 404 }))))
    const { onError } = openMenu({ outputs: { 'sketch-artist': '<p>diagram</p>' } })
    fireEvent.click(allItem())
    await waitFor(() => expect(onError).toHaveBeenLastCalledWith('Download failed'))
    expect(saveBlob).not.toHaveBeenCalled()
  })
})
