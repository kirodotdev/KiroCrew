import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createRequire } from 'node:module'
import type { MouseEvent } from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import DiffBlock, { vscodeFileUrl } from '../components/DiffBlock'
import type { PierreDiffOptions } from '../pierre/config'
import { __resetPathKindCache } from '../hooks/usePathKind'
import { pathKindsFetch } from './pathKindStub'

// The desktop shell's own allowlist: a link this block builds must pass it.
const { isEditorLink } = createRequire(import.meta.url)('../../electron/external-scheme.js') as {
  isEditorLink: (url: string) => boolean
}

// A gateway that takes this browser for a local client and can open its desktop's handlers, seen from a Mac.
vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Kiro Crew', avatar: '/logo.png', directLocal: true, desktopLauncher: true }),
}))
vi.mock('../utils/platform', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/platform')>()),
  isMac: true,
}))
// Pierre's body as one new-side and one old-side line number to click, saying
// whether the block handed it a line-number handler.
type LineNumberClick = Parameters<NonNullable<PierreDiffOptions['onLineNumberClick']>>[0]
vi.mock('../pierre', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierrePatch: ({ options }: { options?: PierreDiffOptions }) => {
    const click = (annotationSide: 'additions' | 'deletions', lineNumber: number) => (e: MouseEvent) =>
      options?.onLineNumberClick?.({ annotationSide, lineNumber, event: e.nativeEvent } as unknown as LineNumberClick)
    return (
      <div data-testid="pierre-body" data-line-links={options?.onLineNumberClick ? 'on' : 'off'}>
        <button onClick={click('additions', 3)}>new 3</button>
        <button onClick={click('deletions', 2)}>old 2</button>
      </div>
    )
  },
}))

const path = '/work/my notes (1).md'
const patch = (hunk: string) => `--- ${path}\n+++ ${path}\n${hunk}\n-old\n+new`
const placed = patch('@@ -1 +1 @@')
const editorUrl = 'vscode://file/work/my%20notes%20%281%29.md'
const openTitle = /^Open .* in side panel$/

/** The shell's bridge in a desktop-app window, answering whether its gateway runs on this machine. */
function shellSays(here: boolean) {
  const available = vi.fn(async () => here)
  vi.stubGlobal('folderPickAPI', { available, pick: vi.fn() })
  return available
}

beforeEach(() => {
  __resetPathKindCache()
  globalThis.fetch = pathKindsFetch(() => 'file') as unknown as typeof fetch
  localStorage.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('DiffBlock editor link', () => {
  it('opens the probed file, or its new-side line, in VS Code on a Cmd+click once the shell says the gateway is here', async () => {
    expect(isEditorLink(vscodeFileUrl(path, 3))).toBe(true)
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    const onFileOpen = vi.fn()

    const available = shellSays(true)
    const local = render(<DiffBlock code={placed} complete onFileOpen={onFileOpen} />)
    fireEvent.click(await screen.findByTitle(openTitle), { metaKey: true })
    await waitFor(() => expect(open).toHaveBeenCalledWith(editorUrl, '_blank', 'noopener,noreferrer'))
    fireEvent.click(screen.getByText('new 3'), { metaKey: true })
    await waitFor(() => expect(open).toHaveBeenLastCalledWith(`${editorUrl}:3`, '_blank', 'noopener,noreferrer'))
    // An old-side number, a plain click on a number and a plain click on Open ask nothing.
    fireEvent.click(screen.getByText('old 2'), { metaKey: true })
    fireEvent.click(screen.getByText('new 3'))
    fireEvent.click(screen.getByTitle(openTitle))
    expect(onFileOpen).toHaveBeenCalledTimes(1)
    expect(onFileOpen).toHaveBeenCalledWith(path)
    expect(available).toHaveBeenCalledTimes(2)
    expect(open).toHaveBeenCalledTimes(2)
    local.unmount()

    // A window whose gateway is on another machine: Cmd+click opens the side panel.
    shellSays(false)
    render(<DiffBlock code={placed} complete onFileOpen={onFileOpen} />)
    fireEvent.click(await screen.findByTitle(openTitle), { metaKey: true })
    await waitFor(() => expect(onFileOpen).toHaveBeenCalledTimes(2))
    expect(open).toHaveBeenCalledTimes(2)
  })

  it('gives line numbers a handler only in the desktop app and where the gutter counts lines of the file', async () => {
    const lineLinks = async (code: string, props: { lineLinks?: boolean } = {}) => {
      const view = render(<DiffBlock code={code} complete onFileOpen={() => {}} {...props} />)
      await screen.findByTitle(openTitle)
      const state = screen.getByTestId('pierre-body').dataset.lineLinks
      view.unmount()
      return state
    }
    shellSays(true)
    expect(await lineLinks(placed)).toBe('on')
    // A tool call's edit card, whose gutter counts the replaced text from 1.
    expect(await lineLinks(placed, { lineLinks: false })).toBe('off')
    // A hunk header without numbers, counted from 1 for display.
    expect(await lineLinks(patch('@@'))).toBe('off')
    // A browser tab: no shell bridge.
    vi.unstubAllGlobals()
    expect(await lineLinks(placed)).toBe('off')
  })
})
