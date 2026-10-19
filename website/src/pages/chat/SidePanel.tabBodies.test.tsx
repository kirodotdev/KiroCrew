import { forwardRef, useImperativeHandle, type ReactNode, type Ref } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { createTestStore } from '../../test/helpers'
import { sseMcpAppRender } from '../../store/chatSlice'
import { i18nT } from '../../i18n/t'
import type { PanelTab } from '../../hooks/usePanelTabs'

/* ── The kept-mounted tab bodies, rendered from their own module with the viewers
 *    stubbed: the cases the panel specs do not reach through `SidePanel` (an MCP
 *    app tab, the turn-diff toolbar, the file rail's in-place navigation, a
 *    restored file whose read throws, a kind with no body). ── */

const rail = vi.hoisted(() => ({ navigated: 0 }))
const read = vi.hoisted(() => ({ fail: null as Error | null }))

vi.mock('../../components/McpAppFrame', () => ({
  default: ({ payload }: { payload: { tool_call_id: string } }) => <div data-testid="mcp-app-frame">{payload.tool_call_id}</div>,
}))
vi.mock('../../components/DiffPanel', () => ({
  default: ({ lineNumbers, sideBySide }: { lineNumbers: boolean; sideBySide: boolean }) => (
    <div data-testid="diff-panel" data-line-numbers={String(lineNumbers)} data-side-by-side={String(sideBySide)} />
  ),
}))
vi.mock('../../components/DetailPanel', () => ({
  default: ({ customHeader, children }: { customHeader: ReactNode; children: ReactNode }) => (
    <div data-testid="detail-panel">{customHeader}{children}</div>
  ),
}))
vi.mock('../../components/MarkdownPanel', () => ({
  default: forwardRef(function MockMarkdownPanel(
    { browserRail }: { browserRail?: ReactNode },
    ref: Ref<{ requestNavigate: (go: (stillClean?: () => boolean) => void) => void }>,
  ) {
    useImperativeHandle(ref, () => ({
      requestNavigate: (go) => { rail.navigated += 1; go(() => true) },
    }))
    return <div data-testid="markdown-panel">{browserRail}</div>
  }),
}))
vi.mock('./FileBrowserRail', () => ({
  default: ({ onFileOpen }: { onFileOpen: (abs: string, diff: boolean, opts?: { line?: number }) => void }) => (
    <button type="button" onClick={() => onFileOpen('/repo/b.ts', false, { line: 3 })}>rail row</button>
  ),
}))
vi.mock('../../utils/fileReadQuery', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/fileReadQuery')>()),
  fetchFileRead: () => (read.fail ? Promise.reject(read.fail) : Promise.resolve({ ok: true, status: 200, text: 'x' })),
}))

import { McpAppTabBody, TabBody } from './SidePanelTabBodies'

const noop = () => {}

function bodyProps(tab: PanelTab, over: Partial<Parameters<typeof TabBody>[0]> = {}): Parameters<typeof TabBody>[0] {
  return {
    tab, active: true, slot: 's1', projectDir: '/repo',
    onClose: noop, onContentChange: noop, onDiskContent: noop, onDiffModeChange: noop,
    onRevealConsumed: noop, onPathChange: noop, onFileSave: async () => {},
    diffLineNumbers: false, setDiffLineNumbers: noop, diffSideBySide: false, setDiffSideBySide: noop,
    ...over,
  }
}

function renderWith(node: ReactNode, store = createTestStore()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <Provider store={store}>{node}</Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  localStorage.clear()
  rail.navigated = 0
  read.fail = null
})
afterEach(() => {
  cleanup()
})

describe('McpAppTabBody', () => {
  const tab: PanelTab = { id: 'app:tc-1', kind: 'app', title: 'Chart', appToolCallId: 'tc-1', slot: 's1' }

  it('says the render is gone when the slot no longer holds its payload', () => {
    renderWith(<McpAppTabBody tab={tab} slot="s1" />)
    expect(screen.getByText(i18nT('pages.chat.sidePanel.this_app_render_is_no_longer_available'))).toBeTruthy()
    expect(screen.queryByTestId('mcp-app-frame')).toBeNull()
  })

  it("mounts the frame for the payload stored under the tab's own slot", () => {
    const store = createTestStore()
    store.dispatch(sseMcpAppRender({
      session_key: 's1', tool_call_id: 'tc-1', server: 'srv', tool: 'chart', html: '<p></p>',
      csp: null, permissions: null, spool_id: 'sp-1',
    }))
    // The panel's active slot is another chat: the tab's own slot decides the payload.
    renderWith(<McpAppTabBody tab={tab} slot="s2" />, store)
    expect(screen.getByTestId('mcp-app-frame').textContent).toBe('tc-1')
  })
})

describe('TabBody', () => {
  it('renders the turn diff toolbar and routes each control', () => {
    const onFileOpen = vi.fn()
    const setDiffSideBySide = vi.fn()
    const setDiffLineNumbers = vi.fn()
    const tab: PanelTab = { id: 'diff:/repo/a.ts', kind: 'diff', title: 'a.ts - Diff', path: '/repo/a.ts', original: 'a\n', modified: 'a\nb\n' }
    renderWith(<TabBody {...bodyProps(tab, { onFileOpen, setDiffSideBySide, setDiffLineNumbers, diffSideBySide: true })} />)
    expect(screen.getByText('a.ts')).toBeTruthy()
    expect(screen.getByText('+1')).toBeTruthy()
    expect(screen.getByTestId('diff-panel').dataset.sideBySide).toBe('true')

    fireEvent.click(screen.getByText('a.ts'))
    fireEvent.click(screen.getByLabelText(i18nT('pages.chat.sidePanel.open_in_editor')))
    expect(onFileOpen.mock.calls).toEqual([['/repo/a.ts'], ['/repo/a.ts']])

    fireEvent.click(screen.getByLabelText(i18nT('pages.chat.sidePanel.switch_to_unified_view')))
    expect(setDiffSideBySide).toHaveBeenCalledTimes(1)
    expect((setDiffSideBySide.mock.calls[0][0] as (v: boolean) => boolean)(true)).toBe(false)

    fireEvent.click(screen.getByLabelText(i18nT('pages.chat.sidePanel.show_line_numbers')))
    expect(setDiffLineNumbers).toHaveBeenCalledTimes(1)
    expect((setDiffLineNumbers.mock.calls[0][0] as (v: boolean) => boolean)(false)).toBe(true)
  })

  it('renders nothing for a kind that has no kept-mounted body', () => {
    const tab: PanelTab = { id: 'changes', kind: 'changes', title: 'Changes' }
    const { container } = renderWith(<TabBody {...bodyProps(tab)} />)
    expect(container.innerHTML).toBe('')
  })

  it("re-targets the file tab in place from its rail, through the editor's dirty guard", () => {
    const onFileOpen = vi.fn()
    const tab: PanelTab = { id: 'file:/repo/a.ts', kind: 'file', title: 'a.ts', path: '/repo/a.ts', content: 'x', savedContent: 'x' }
    renderWith(<TabBody {...bodyProps(tab, { onFileOpen })} />)
    fireEvent.click(screen.getByText('rail row'))
    expect(rail.navigated).toBe(1)
    expect(onFileOpen).toHaveBeenCalledTimes(1)
    const [abs, opts] = onFileOpen.mock.calls[0]
    expect(abs).toBe('/repo/b.ts')
    expect(opts).toMatchObject({ diffMode: false, line: 3, replaceId: 'file:/repo/a.ts' })
    expect(opts.canReplace()).toBe(true)
  })

  it('reports a restored file whose read throws, naming the file and the reason', async () => {
    read.fail = new Error('socket hang up')
    const tab: PanelTab = { id: 'file:/repo/c.ts', kind: 'file', title: 'c.ts', path: '/repo/c.ts' }
    renderWith(<TabBody {...bodyProps(tab)} />)
    const notice = await screen.findByTestId('file-tab-hydration-error')
    expect(notice.textContent).toContain('/repo/c.ts')
    expect(notice.textContent).toContain('socket hang up')
  })
})
