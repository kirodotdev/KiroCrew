/**
 * The "Open file" chip an assistant reply shows for each file it names with an
 * open-file marker (OpenFileChips, rendered by AssistantMessage).
 *
 * The chip only ever opens on a click, and the click must be today's
 * click-to-open: the same handler the reply's path chips get, called with the
 * same arguments a path-chip click passes, landing as a plain file tab through
 * the page's real open handler (usePanelDocumentActions) with no diff.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, renderHook, waitFor } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'

// Records the props AssistantMessage hands its MarkdownRenderer, so a test can
// compare the chip's handler with the one the reply's path chips receive.
const md = vi.hoisted(() => ({ props: [] as Array<{ content: string; onFileOpen?: unknown }> }))
// Only the component is replaced: the module's helpers (the marker parser uses
// its `fixCodeFences`) stay real.
vi.mock('../components/MarkdownRenderer', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../components/MarkdownRenderer')>()),
  default: (props: { content: string; onFileOpen?: unknown }) => {
    md.props.push(props)
    return <div data-testid="md">{props.content}</div>
  },
}))
// Passthrough: the smoothing rAF loop is not under test here.
vi.mock('../hooks/useSmoothStream', () => ({ useSmoothStream: (content: string) => content }))
const fileRead = vi.hoisted(() => ({ fetch: vi.fn() }))
vi.mock('../utils/fileReadQuery', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/fileReadQuery')>()),
  fetchFileRead: fileRead.fetch,
}))

import AssistantMessage from '../pages/chat/AssistantMessage'
import { activatePath } from '../components/markdown/pathReferences'
import { api } from '../api/client'
import { usePanelDocumentActions } from '../hooks/usePanelDocumentActions'

const NOTE = '/home/u/notes/today.md'
const marker = (path: string) => `<!-- kirocrew:open-file path="${path}" -->`
const reply = `Wrote the note.\n${marker(NOTE)}\n`
/** The chip's accessible name is its visible text: the label, then the file name. */
const chipName = (path: string) => `Open file ${path.split('/').pop()}`
const okRead = (text: string) => ({ text, ok: true, status: 200, binary: false, truncated: false, redacted: false, lossy: false })

/** The page's real file opener, over spy tab controls, as ChatPage wires it. */
function realOpener() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tabsCtl = { openFile: vi.fn(), openWorkingTreeDiff: vi.fn() }
  const showActionError = vi.fn()
  const onOpened = vi.fn()
  const { result } = renderHook(() => usePanelDocumentActions({
    tabsCtl: tabsCtl as never, slotRef: { current: 'slot-a' }, queryClient, showActionError, onOpened,
  }))
  return { openFile: result.current.openFile, tabsCtl, showActionError, onOpened }
}

beforeEach(() => {
  md.props.length = 0
  fileRead.fetch.mockReset().mockResolvedValue(okRead('# notes'))
  vi.spyOn(api, 'fileDiff').mockResolvedValue({} as never)
  delete (window as { __kirocrewPluginHandlesFiles?: boolean }).__kirocrewPluginHandlesFiles
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('Open file chip', () => {
  it('renders an Open file chip for a file the agent names in its reply', () => {
    render(<AssistantMessage content={reply} isStreaming={false} slotRunning={false} onFileOpen={vi.fn()} />)
    const chip = screen.getByRole('button', { name: chipName(NOTE) })
    expect(chip).toHaveTextContent('Open file')
    expect(chip).toHaveTextContent('today.md')
    // The full path is the tooltip and the accessible description.
    expect(chip).toHaveAttribute('title', NOTE)
    expect(chip).toHaveAccessibleDescription(NOTE)
  })

  it('stacks one chip per row, never a row of three or more buttons', () => {
    const three = `${reply}${marker('/home/u/notes/plan.md')}\n${marker('/home/u/notes/log.md')}`
    render(<AssistantMessage content={three} isStreaming={false} slotRunning={false} onFileOpen={vi.fn()} />)
    const group = screen.getByTestId('open-file-chips')
    expect(group.querySelectorAll('button')).toHaveLength(3)
    expect(group).toHaveClass('flex-col')
    expect(group).not.toHaveClass('flex-wrap')
  })

  it('renders one chip per named file, and none without a marker or a file handler', () => {
    const two = `${reply}${marker('/home/u/notes/plan.md')}`
    const { unmount } = render(<AssistantMessage content={two} isStreaming={false} slotRunning={false} onFileOpen={vi.fn()} />)
    expect(screen.getAllByRole('button', { name: /^Open file .*\.md$/ })).toHaveLength(2)
    unmount()
    const plain = render(<AssistantMessage content={`Wrote ${NOTE}.`} isStreaming={false} slotRunning={false} onFileOpen={vi.fn()} />)
    expect(screen.queryByTestId('open-file-chips')).toBeNull()
    plain.unmount()
    render(<AssistantMessage content={reply} isStreaming={false} slotRunning={false} />)
    expect(screen.queryByTestId('open-file-chips')).toBeNull()
  })

  it('a click calls the handler the path chips get, with the arguments a path-chip click passes', () => {
    const onFileOpen = vi.fn()
    render(<AssistantMessage content={reply} isStreaming={false} slotRunning={false} onFileOpen={onFileOpen} />)
    // The chip and the reply's path chips are handed the same function.
    expect(md.props.at(-1)?.onFileOpen).toBe(onFileOpen)
    fireEvent.click(screen.getByRole('button', { name: chipName(NOTE) }))
    // Today's click on a file path chip, through the chip's own activation.
    const pathChipOpen = vi.fn()
    activatePath(NOTE, 'file', false, { onFileOpen: pathChipOpen }, () => {})
    expect(pathChipOpen.mock.calls).toEqual([[NOTE]])
    expect(onFileOpen.mock.calls).toEqual(pathChipOpen.mock.calls)
  })

  it('opens nothing on render, while streaming, or on a reload', async () => {
    const onFileOpen = vi.fn()
    const onOpenDiff = vi.fn()
    const live = render(<AssistantMessage content={reply} isStreaming slotRunning onFileOpen={onFileOpen} onOpenDiff={onOpenDiff} />)
    // Mid-stream: no chip yet, and nothing opened.
    expect(screen.queryByTestId('open-file-chips')).toBeNull()
    live.rerender(<AssistantMessage content={reply} isStreaming={false} slotRunning={false} onFileOpen={onFileOpen} onOpenDiff={onOpenDiff} />)
    // The turn ends: the chip shows, still nothing opened.
    expect(screen.getByRole('button', { name: chipName(NOTE) })).toBeInTheDocument()
    live.unmount()
    // A reload remounts the same finished reply: the chip is back, nothing opens.
    render(<AssistantMessage content={reply} isStreaming={false} slotRunning={false} onFileOpen={onFileOpen} onOpenDiff={onOpenDiff} />)
    expect(screen.getByRole('button', { name: chipName(NOTE) })).toBeInTheDocument()
    expect(onFileOpen).not.toHaveBeenCalled()
    expect(onOpenDiff).not.toHaveBeenCalled()
    // Through the page's real opener too: rendering the chip reads no file.
    const real = realOpener()
    render(<AssistantMessage content={reply} isStreaming={false} slotRunning={false} onFileOpen={real.openFile} />)
    expect(fileRead.fetch).not.toHaveBeenCalled()
    expect(real.tabsCtl.openFile).not.toHaveBeenCalled()
  })

  it('a click opens the file as a plain tab through the real open handler, as a direct click-to-open does, with no diff', async () => {
    // Today's click-to-open, called directly: the reference.
    const direct = realOpener()
    await direct.openFile(NOTE)
    expect(direct.tabsCtl.openFile).toHaveBeenCalledTimes(1)
    const directReads = fileRead.fetch.mock.calls.map(call => [call[0], call.length])
    fileRead.fetch.mockClear()

    const viaChip = realOpener()
    const onOpenDiff = vi.fn()
    render(<AssistantMessage content={reply} isStreaming={false} slotRunning={false} onFileOpen={viaChip.openFile} onOpenDiff={onOpenDiff} />)
    fireEvent.click(screen.getByRole('button', { name: chipName(NOTE) }))
    await waitFor(() => expect(viaChip.tabsCtl.openFile).toHaveBeenCalledTimes(1))

    // Same read, same tab, same reveal as the direct open.
    expect(fileRead.fetch.mock.calls.map(call => [call[0], call.length])).toEqual(directReads)
    expect(viaChip.tabsCtl.openFile.mock.calls).toEqual(direct.tabsCtl.openFile.mock.calls)
    expect(viaChip.tabsCtl.openFile.mock.calls[0].slice(0, 3)).toEqual([NOTE, '# notes', 'slot-a'])
    expect(viaChip.onOpened).toHaveBeenCalledTimes(1)
    // A plain file tab: no diff tab, no diff mode, no diff card.
    expect(viaChip.tabsCtl.openWorkingTreeDiff).not.toHaveBeenCalled()
    expect(viaChip.tabsCtl.openFile.mock.calls[0][3]?.diffMode).toBeFalsy()
    expect(onOpenDiff).not.toHaveBeenCalled()
    expect(viaChip.showActionError).not.toHaveBeenCalled()
  })
})
