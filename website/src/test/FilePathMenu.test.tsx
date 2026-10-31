import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import FilePathMenu, { remoteEditorUrl } from '../components/FilePathMenu'
import {
  consumeChatHandoff,
  installSoftNavigate,
  __resetErrorJournalForTests,
  __resetNavSeamForTests,
} from '../utils/errorReport'

// ── Mocks ────────────────────────────────────────────────────────────────────

const brandingEnv = vi.hoisted(() => ({ directLocal: true, remoteEditor: { editor: '', host: '' } }))
const platformEnv = vi.hoisted(() => ({ value: 'other' as 'other' | 'darwin' | 'windows' }))
const embeddedEnv = vi.hoisted(() => ({ value: false }))
const electronEnv = vi.hoisted(() => ({ isElectron: false }))
const editorEnv = vi.hoisted(() => ({
  enabled: false,
  open: vi.fn(),
}))

vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: brandingEnv.directLocal, remoteEditor: brandingEnv.remoteEditor }),
}))

// The reveal label is platform-aware (names Finder / File Explorer on the
// gateway's own OS). Drive it explicitly so the label assertions are stable
// regardless of the test host.
vi.mock('../hooks/useGatewayPlatform', () => ({
  useGatewayPlatform: () => platformEnv.value,
}))

// An embedded instance pane (the dashboard inside InstancesViewport's iframe)
// cannot hand a custom scheme to the OS, so the remote-editor row hides there.
vi.mock('../lib/embedded', () => ({
  isEmbeddedPane: () => embeddedEnv.value,
}))

vi.mock('../lib/electron', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/electron')>()),
  get isElectron() { return electronEnv.isElectron },
  canOpenFileInEditor: () => editorEnv.enabled,
  openFileInEditor: editorEnv.open,
}))

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      revealPath: vi.fn().mockResolvedValue({ ok: true }),
    },
  }
})

vi.mock('../utils/clipboard', () => ({
  // Resolves TRUE by default: the real signature is `Promise<boolean>`, and a
  // bare `vi.fn()` returning undefined is falsy, so it could not tell a
  // successful clipboard write from a failed one.
  copyToClipboard: vi.fn().mockResolvedValue(true),
}))

import { api } from '../api/client'
import { copyToClipboard } from '../utils/clipboard'

// ── Helpers ──────────────────────────────────────────────────────────────────

beforeEach(() => {
  vi.clearAllMocks()
  brandingEnv.directLocal = true
  brandingEnv.remoteEditor = { editor: '', host: '' }
  embeddedEnv.value = false
  electronEnv.isElectron = false
  platformEnv.value = 'other'
  editorEnv.enabled = false
  editorEnv.open.mockResolvedValue({ ok: true })
  vi.mocked(api.revealPath).mockResolvedValue({ ok: true })
  vi.mocked(copyToClipboard).mockResolvedValue(true)
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  sessionStorage.clear()
  installSoftNavigate(() => {})
})

afterEach(() => {
  // Restored here, not in the test's own finally: a failing assertion between
  // useFakeTimers and the end of the test would otherwise leave every later
  // test on fake timers (docs/testing.md, "Fake timers go on after the render
  // settles, and off in afterEach").
  vi.useRealTimers()
  brandingEnv.directLocal = true
  brandingEnv.remoteEditor = { editor: '', host: '' }
  platformEnv.value = 'other'
  editorEnv.enabled = false
  __resetNavSeamForTests()
})

function rightClick(el: Element) {
  fireEvent.contextMenu(el)
}

// ── FilePathMenu (right-click wrapper) ───────────────────────────────────────

describe('FilePathMenu', () => {
  const TEST_PATH = '/home/user/project/report.md'

  describe('when directLocal is true', () => {
    it('renders all three items: open, reveal, copy path', async () => {
      renderWithProviders(
        <FilePathMenu filePath={TEST_PATH}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )

      rightClick(screen.getByTestId('trigger'))

      await waitFor(() => {
        expect(screen.getByText('Open with default app')).toBeInTheDocument()
      })
      expect(screen.getByText('Show in file manager')).toBeInTheDocument()
      expect(screen.getByText('Copy path')).toBeInTheDocument()
    })

    it('calls revealPath with "open" when Open item is selected', async () => {
      renderWithProviders(
        <FilePathMenu filePath={TEST_PATH}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )

      rightClick(screen.getByTestId('trigger'))

      await waitFor(() => {
        expect(screen.getByText('Open with default app')).toBeInTheDocument()
      })
      fireEvent.click(screen.getByText('Open with default app'))

      await waitFor(() => {
        expect(api.revealPath).toHaveBeenCalledWith(TEST_PATH, 'open')
      })
    })

    it('calls revealPath with "reveal" when Reveal item is selected', async () => {
      renderWithProviders(
        <FilePathMenu filePath={TEST_PATH}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )

      rightClick(screen.getByTestId('trigger'))

      await waitFor(() => {
        expect(screen.getByText('Show in file manager')).toBeInTheDocument()
      })
      fireEvent.click(screen.getByText('Show in file manager'))

      await waitFor(() => {
        expect(api.revealPath).toHaveBeenCalledWith(TEST_PATH, 'reveal')
      })
    })

    it('calls copyToClipboard when Copy path item is selected', async () => {
      renderWithProviders(
        <FilePathMenu filePath={TEST_PATH}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )

      rightClick(screen.getByTestId('trigger'))

      await waitFor(() => {
        expect(screen.getByText('Copy path')).toBeInTheDocument()
      })
      fireEvent.click(screen.getByText('Copy path'))

      expect(copyToClipboard).toHaveBeenCalledWith(TEST_PATH)
    })
  })

  describe('when directLocal is false (remote session)', () => {
    beforeEach(() => { brandingEnv.directLocal = false })

    it('hides open and reveal items, shows only copy path', async () => {
      renderWithProviders(
        <FilePathMenu filePath={TEST_PATH}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )

      rightClick(screen.getByTestId('trigger'))

      await waitFor(() => {
        expect(screen.getByText('Copy path')).toBeInTheDocument()
      })
      expect(screen.queryByText('Open with default app')).not.toBeInTheDocument()
      expect(screen.queryByText('Show in file manager')).not.toBeInTheDocument()
    })
  })

  // A direct-local HEADLESS session (e.g. SSH loopback) is still directLocal, so
  // the Open/Reveal rows render — but files.py has no desktop to drive and
  // degrades the action to a clipboard copy (`{ copy }`). The row must
  // acknowledge that copy through the same inline "Path copied" swap the Copy
  // row uses, rather than silently copying under a "Show in file manager" label.
  describe('reveal/open degrades to a clipboard copy (headless direct-local)', () => {
    it('acknowledges the copy with "Path copied" instead of a silent no-op', async () => {
      ;(api.revealPath as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
        ok: true,
        copy: TEST_PATH,
      })

      renderWithProviders(
        <FilePathMenu filePath={TEST_PATH}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )

      rightClick(screen.getByTestId('trigger'))

      await waitFor(() => {
        expect(screen.getByText('Show in file manager')).toBeInTheDocument()
      })
      fireEvent.click(screen.getByText('Show in file manager'))

      // The backend degraded to a copy: the path is written and the menu's Copy
      // row flips to the "Path copied" acknowledgment (menu stays open on select).
      await waitFor(() => {
        expect(copyToClipboard).toHaveBeenCalledWith(TEST_PATH)
      })
      await waitFor(() => {
        expect(screen.getByText('Path copied')).toBeInTheDocument()
      })
    })

    it('flips to the copy-failed wording when the clipboard write fails', async () => {
      // A degrade that could not reach the clipboard must not be acknowledged as
      // a copy — the row would claim success while the clipboard still holds the
      // previous contents.
      ;(copyToClipboard as ReturnType<typeof vi.fn>).mockResolvedValueOnce(false)
      ;(api.revealPath as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
        ok: true,
        copy: TEST_PATH,
      })

      renderWithProviders(
        <FilePathMenu filePath={TEST_PATH}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )

      rightClick(screen.getByTestId('trigger'))

      await waitFor(() => {
        expect(screen.getByText('Show in file manager')).toBeInTheDocument()
      })
      fireEvent.click(screen.getByText('Show in file manager'))

      await waitFor(() => {
        expect(screen.getByText('Couldn’t copy the path')).toBeInTheDocument()
      })
      expect(screen.queryByText('Path copied')).not.toBeInTheDocument()
    })

    it('flips the Copy row to the failed wording when the write returns false', async () => {
      // Same defect, second site: `copyPath`'s try/catch only ever saw the
      // throwing branch, so a `false` return read as success.
      ;(copyToClipboard as ReturnType<typeof vi.fn>).mockResolvedValueOnce(false)

      renderWithProviders(
        <FilePathMenu filePath={TEST_PATH}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )

      rightClick(screen.getByTestId('trigger'))

      await waitFor(() => {
        expect(screen.getByText('Copy path')).toBeInTheDocument()
      })
      fireEvent.click(screen.getByText('Copy path'))

      await waitFor(() => {
        expect(screen.getByText('Couldn’t copy the path')).toBeInTheDocument()
      })
      expect(screen.queryByText('Path copied')).not.toBeInTheDocument()
    })
  })

  describe('menu error hand-offs', () => {
    it.each([
      ['Enter', '{Enter}'],
      ['Space', ' '],
    ])('keeps reveal activation on its row and stages the failure with %s', async (_label, key) => {
      const user = userEvent.setup()
      vi.mocked(api.revealPath).mockRejectedValue(new Error('reveal refused'))
      renderWithProviders(
        <FilePathMenu filePath={TEST_PATH}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )

      rightClick(screen.getByTestId('trigger'))
      const reveal = await screen.findByRole('menuitem', { name: 'Show in file manager' })
      reveal.focus()
      await user.keyboard('{Enter}')
      const alert = await screen.findByTestId('file-path-menu-error')
      expect(api.revealPath).toHaveBeenCalledTimes(1)

      reveal.focus()
      await user.keyboard('{Enter}')
      await waitFor(() => expect(api.revealPath).toHaveBeenCalledTimes(2))
      await screen.findByTestId('file-path-menu-error')

      reveal.focus()
      await user.keyboard('{ArrowDown}')
      const handoff = screen.getByRole('menuitem', { name: /^ask the agent$/i })
      expect(handoff).toHaveFocus()
      expect(handoff).toHaveAttribute('aria-describedby', alert.id)
      expect(handoff.getAttribute('title')).toBeTruthy()
      expect(handoff.querySelector('svg')).toHaveClass('text-muted')
      await user.keyboard(key)

      expect(consumeChatHandoff()).toContain('Couldn’t open this path on the machine running Kiro Crew.')
      expect(api.revealPath).toHaveBeenCalledTimes(2)
    })

    it('gives an editor-launch failure its own described hand-off item', async () => {
      const user = userEvent.setup()
      editorEnv.enabled = true
      editorEnv.open.mockResolvedValue({ ok: false, error: 'editor refused' })
      renderWithProviders(
        <FilePathMenu filePath={TEST_PATH}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )

      rightClick(screen.getByTestId('trigger'))
      const openInEditor = await screen.findByRole('menuitem', { name: 'Open in editor' })
      openInEditor.focus()
      await user.keyboard('{Enter}')
      const alert = await screen.findByTestId('file-path-menu-editor-error')

      openInEditor.focus()
      await user.keyboard('{ArrowDown}')
      const handoff = screen.getByRole('menuitem', { name: /^ask the agent$/i })
      expect(handoff).toHaveFocus()
      expect(handoff).toHaveAttribute('aria-describedby', alert.id)
      await user.keyboard('{Enter}')

      expect(consumeChatHandoff()).toContain('editor refused')
      expect(editorEnv.open).toHaveBeenCalledTimes(1)
    })
  })

  describe('platform-aware reveal label', () => {
    it('names Finder on a macOS gateway', async () => {
      platformEnv.value = 'darwin'
      renderWithProviders(
        <FilePathMenu filePath={TEST_PATH}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )

      rightClick(screen.getByTestId('trigger'))

      await waitFor(() => {
        expect(screen.getByText('Open in Finder')).toBeInTheDocument()
      })
      // The reveal label follows the gateway OS; the generic wording is gone.
      expect(screen.queryByText('Show in file manager')).not.toBeInTheDocument()
      fireEvent.click(screen.getByText('Open in Finder'))
      await waitFor(() => {
        expect(api.revealPath).toHaveBeenCalledWith(TEST_PATH, 'reveal')
      })
    })

    it('names File Explorer on a Windows gateway', async () => {
      platformEnv.value = 'windows'
      renderWithProviders(
        <FilePathMenu filePath={TEST_PATH}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )

      rightClick(screen.getByTestId('trigger'))

      await waitFor(() => {
        expect(screen.getByText('Open in File Explorer')).toBeInTheDocument()
      })
    })
  })

  describe('Windows suppresses "Open with default app"', () => {
    // The gateway's files.py refuses the launch-by-association verb on Windows
    // and degrades an `open` to a clipboard copy, so the row must not appear
    // there — it would promise a launch the backend never performs. Reveal
    // (which does work) and Copy path stay.
    it('hides Open on a Windows gateway but keeps reveal + copy', async () => {
      platformEnv.value = 'windows'
      renderWithProviders(
        <FilePathMenu filePath={TEST_PATH}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )

      rightClick(screen.getByTestId('trigger'))

      await waitFor(() => {
        expect(screen.getByText('Open in File Explorer')).toBeInTheDocument()
      })
      expect(screen.queryByText('Open with default app')).not.toBeInTheDocument()
      expect(screen.getByText('Copy path')).toBeInTheDocument()
    })

    it('shows Open on a macOS gateway', async () => {
      platformEnv.value = 'darwin'
      renderWithProviders(
        <FilePathMenu filePath={TEST_PATH}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )

      rightClick(screen.getByTestId('trigger'))

      await waitFor(() => {
        expect(screen.getByText('Open with default app')).toBeInTheDocument()
      })
    })
  })

  describe('directory paths', () => {
    it('hides "Open with default app" for a directory but keeps reveal + copy', async () => {
      renderWithProviders(
        <FilePathMenu filePath="/home/user/project" kind="dir">
          <span data-testid="trigger">project</span>
        </FilePathMenu>,
      )

      rightClick(screen.getByTestId('trigger'))

      await waitFor(() => {
        expect(screen.getByText('Show in file manager')).toBeInTheDocument()
      })
      expect(screen.getByText('Copy path')).toBeInTheDocument()
      // A directory cannot be "opened" — /api/reveal 400s an open on a dir.
      expect(screen.queryByText('Open with default app')).not.toBeInTheDocument()
    })
  })
})

// ── Remote-editor row (remote gateway → local editor over SSH) ───────────────

describe('FilePathMenu remote-editor row', () => {
  const REMOTE_PATH = '/home/user/.kiro/crew/config.json'

  beforeEach(() => {
    brandingEnv.directLocal = false
    brandingEnv.remoteEditor = { editor: 'vscode', host: 'dev-dsk.example.com' }
  })

  it('shows "Open in VS Code" on a remote session with editor + host set', async () => {
    renderWithProviders(
      <FilePathMenu filePath={REMOTE_PATH}>
        <span data-testid="trigger">config.json</span>
      </FilePathMenu>,
    )
    rightClick(screen.getByTestId('trigger'))
    await waitFor(() => expect(screen.getByText('Open in VS Code')).toBeInTheDocument())
  })

  it('labels the row per configured editor', async () => {
    brandingEnv.remoteEditor = { editor: 'kiro', host: 'h.example.com' }
    renderWithProviders(
      <FilePathMenu filePath={REMOTE_PATH}>
        <span data-testid="trigger">config.json</span>
      </FilePathMenu>,
    )
    rightClick(screen.getByTestId('trigger'))
    await waitFor(() => expect(screen.getByText('Open in Kiro')).toBeInTheDocument())
  })

  it('is shown on a direct-local session too (an ssh -L forward reads as direct-local)', async () => {
    // direct_local is true for a browser reaching a headless gateway through an
    // `ssh -L` forward, the main topology this row serves, so the row gates on
    // its own configuration alone.
    brandingEnv.directLocal = true
    renderWithProviders(
      <FilePathMenu filePath={REMOTE_PATH}>
        <span data-testid="trigger">config.json</span>
      </FilePathMenu>,
    )
    rightClick(screen.getByTestId('trigger'))
    await waitFor(() => expect(screen.getByText('Open in VS Code')).toBeInTheDocument())
  })

  it('is grouped with the editor launches, ahead of Open and Copy path', async () => {
    brandingEnv.directLocal = true
    renderWithProviders(
      <FilePathMenu filePath={REMOTE_PATH}>
        <span data-testid="trigger">config.json</span>
      </FilePathMenu>,
    )
    rightClick(screen.getByTestId('trigger'))
    await waitFor(() => expect(screen.getByText('Open in VS Code')).toBeInTheDocument())
    const labels = screen.getAllByRole('menuitem').map(el => el.textContent ?? '')
    const editorAt = labels.findIndex(l => l.includes('Open in VS Code'))
    const openAt = labels.findIndex(l => l.includes('Open with default app'))
    const copyAt = labels.findIndex(l => l.includes('Copy path'))
    expect(editorAt).toBeGreaterThanOrEqual(0)
    // Grouped with the editor launches: ahead of the gateway-host Open row and
    // the clipboard fallback.
    expect(editorAt).toBeLessThan(openAt)
    expect(editorAt).toBeLessThan(copyAt)
  })

  it('is hidden when no editor is configured (feature off)', async () => {
    brandingEnv.remoteEditor = { editor: '', host: 'h.example.com' }
    renderWithProviders(
      <FilePathMenu filePath={REMOTE_PATH}>
        <span data-testid="trigger">config.json</span>
      </FilePathMenu>,
    )
    rightClick(screen.getByTestId('trigger'))
    await waitFor(() => expect(screen.getByText('Copy path')).toBeInTheDocument())
    expect(screen.queryByText(/^Open in /)).not.toBeInTheDocument()
  })

  it('is hidden when no host is configured (no malformed link)', async () => {
    brandingEnv.remoteEditor = { editor: 'vscode', host: '' }
    renderWithProviders(
      <FilePathMenu filePath={REMOTE_PATH}>
        <span data-testid="trigger">config.json</span>
      </FilePathMenu>,
    )
    rightClick(screen.getByTestId('trigger'))
    await waitFor(() => expect(screen.getByText('Copy path')).toBeInTheDocument())
    expect(screen.queryByText('Open in VS Code')).not.toBeInTheDocument()
  })

  it('is hidden inside an embedded instance pane (frame CSP blocks the scheme)', async () => {
    brandingEnv.remoteEditor = { editor: 'vscode', host: 'dev-host' }
    embeddedEnv.value = true
    renderWithProviders(
      <FilePathMenu filePath={REMOTE_PATH}>
        <span data-testid="trigger">config.json</span>
      </FilePathMenu>,
    )
    rightClick(screen.getByTestId('trigger'))
    await waitFor(() => expect(screen.getByText('Copy path')).toBeInTheDocument())
    expect(screen.queryByText('Open in VS Code')).not.toBeInTheDocument()
  })

  it('is hidden in Electron while Copy path remains available', async () => {
    electronEnv.isElectron = true
    renderWithProviders(
      <FilePathMenu filePath={REMOTE_PATH}>
        <span data-testid="trigger">config.json</span>
      </FilePathMenu>,
    )
    rightClick(screen.getByTestId('trigger'))
    await waitFor(() => expect(screen.getByText('Copy path')).toBeInTheDocument())
    expect(screen.queryByText('Open in VS Code')).not.toBeInTheDocument()
  })

  it('is hidden for an unencodable path while Copy path remains available', async () => {
    const unencodablePath = '/home/u/bad\udcffname'
    expect(() => renderWithProviders(
      <FilePathMenu filePath={unencodablePath}>
        <span data-testid="trigger">bad name</span>
      </FilePathMenu>,
    )).not.toThrow()

    rightClick(screen.getByTestId('trigger'))
    await waitFor(() => expect(screen.getByText('Copy path')).toBeInTheDocument())
    expect(screen.queryByText('Open in VS Code')).not.toBeInTheDocument()
  })

  it('is shown for a directory (opens as a remote workspace)', async () => {
    renderWithProviders(
      <FilePathMenu filePath="/home/user/.kiro/crew" kind="dir">
        <span data-testid="trigger">crew</span>
      </FilePathMenu>,
    )
    rightClick(screen.getByTestId('trigger'))
    // Unlike "Open with default app", the remote-editor row is NOT gated on kind.
    await waitFor(() => expect(screen.getByText('Open in VS Code')).toBeInTheDocument())
  })

  it('routes the vscode-remote URL through window.location on select', async () => {
    renderWithProviders(
      <FilePathMenu filePath={REMOTE_PATH}>
        <span data-testid="trigger">config.json</span>
      </FilePathMenu>,
    )
    rightClick(screen.getByTestId('trigger'))
    const row = await screen.findByRole('menuitem', { name: 'Open in VS Code' })
    // Stub window.location AFTER render so the router is unaffected; capture the
    // href the row assigns (openRemoteEditor uses window.location.href, not an
    // <a> the markdown link rewriter would rewrite — see issue #9925).
    const orig = Object.getOwnPropertyDescriptor(window, 'location')!
    let assigned = ''
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { get href() { return '' }, set href(v: string) { assigned = v } },
    })
    try {
      fireEvent.click(row)
    } finally {
      Object.defineProperty(window, 'location', orig)
    }
    expect(assigned).toBe(
      'vscode://vscode-remote/ssh-remote+dev-dsk.example.com/home/user/.kiro/crew/config.json',
    )
  })

  // A custom-scheme navigation is fire-and-forget: with no handler registered
  // (or SSH failing inside the editor) the browser drops it silently and the
  // page learns nothing. The row is the only surface that can say anything, so
  // it acknowledges the click in place the way the Copy row's "Path copied"
  // does — and the menu must stay open for that to be readable. Unlike "Path
  // copied" the hint carries the troubleshooting line, so it must not time out
  // under the reader: it lasts as long as the menu does.
  it('acknowledges the click in place and keeps the menu open, then resets', async () => {
    renderWithProviders(
      <FilePathMenu filePath={REMOTE_PATH}>
        <span data-testid="trigger">config.json</span>
      </FilePathMenu>,
    )
    rightClick(screen.getByTestId('trigger'))
    const row = await screen.findByRole('menuitem', { name: 'Open in VS Code' })
    // Fake timers only AFTER the last real-timer wait (see docs/testing.md), so
    // the menu-open waitFor above is not starved.
    vi.useFakeTimers()
    const orig = Object.getOwnPropertyDescriptor(window, 'location')!
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { get href() { return '' }, set href(_v: string) { /* swallowed */ } },
    })
    try {
      fireEvent.click(row)
    } finally {
      Object.defineProperty(window, 'location', orig)
    }
    // Label swapped to the sent-plus-troubleshooting hint, naming the editor…
    const hint = screen.getByRole('menuitem', { name: /^Sent to VS Code\. Nothing opened\?/ })
    expect(hint).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Open in VS Code' })).not.toBeInTheDocument()
    // …and the menu did NOT close (preventDefault on select, like the Copy row):
    // its other rows are still on screen.
    expect(screen.getByRole('menuitem', { name: 'Copy path' })).toBeInTheDocument()
    // The hint is not on a timer: well past the old 2.5 s acknowledgment window
    // it is still there to be read while the menu stays open.
    act(() => { vi.advanceTimersByTime(10_000) })
    expect(screen.getByRole('menuitem', { name: /^Sent to VS Code/ })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Open in VS Code' })).not.toBeInTheDocument()
    // Closing the menu is what ends it: reopened, the row offers "Open in VS
    // Code" again rather than a stale status. Back on real timers for the
    // Radix close/open round trip, which waitFor cannot drive under fake ones.
    vi.useRealTimers()
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })
    await waitFor(() => {
      expect(screen.queryByRole('menuitem', { name: /^Sent to VS Code/ })).not.toBeInTheDocument()
    })
    rightClick(screen.getByTestId('trigger'))
    expect(await screen.findByRole('menuitem', { name: 'Open in VS Code' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: /^Sent to VS Code/ })).not.toBeInTheDocument()
  })

  // The label swap above is an in-place text change on a row, which a screen
  // reader does not announce on its own: an aria-label rewrite is silent. The
  // hint therefore ALSO lands in a polite live region (RemoteEditorSentStatus,
  // shared with the two overflow menus), mounted empty when the menu opens so
  // the later text change is what gets read out.
  it('announces the sent hint through a polite live region a screen reader can hear', async () => {
    renderWithProviders(
      <FilePathMenu filePath={REMOTE_PATH}>
        <span data-testid="trigger">config.json</span>
      </FilePathMenu>,
    )
    rightClick(screen.getByTestId('trigger'))
    const row = await screen.findByRole('menuitem', { name: 'Open in VS Code' })
    // Before the click: the region exists (so the change is announced) but is
    // empty, so opening the menu announces nothing.
    const region = screen.getByRole('status')
    expect(region).toHaveAttribute('aria-live', 'polite')
    expect(region).toHaveClass('sr-only')
    expect(region).toHaveTextContent('')
    const orig = Object.getOwnPropertyDescriptor(window, 'location')!
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { get href() { return '' }, set href(_v: string) { /* swallowed */ } },
    })
    try {
      fireEvent.click(row)
    } finally {
      Object.defineProperty(window, 'location', orig)
    }
    // After the click: the same hint the row shows is in the live region.
    expect(screen.getByRole('status')).toHaveTextContent(/^Sent to VS Code\. Nothing opened\?/)
    // The row's own text is still there too; the region adds the announcement,
    // it does not replace the visible acknowledgment.
    expect(screen.getByRole('menuitem', { name: /^Sent to VS Code/ })).toBeInTheDocument()
  })
})

// ── remoteEditorUrl (pure URL builder) ───────────────────────────────────────

describe('remoteEditorUrl', () => {
  it('builds the vscode-remote authority URL', () => {
    expect(remoteEditorUrl('vscode', 'host.example.com', '/a/b.txt')).toBe(
      'vscode://vscode-remote/ssh-remote+host.example.com/a/b.txt',
    )
  })

  it('uses each editor id as its scheme', () => {
    expect(remoteEditorUrl('kiro', 'h', '/x')).toBe('kiro://vscode-remote/ssh-remote+h/x')
  })

  it('percent-encodes path segments but keeps the separators', () => {
    // A space and a `#` in a filename must not break out of the path.
    expect(remoteEditorUrl('vscode', 'h', '/a/my file #1.md')).toBe(
      'vscode://vscode-remote/ssh-remote+h/a/my%20file%20%231.md',
    )
  })

  it('returns null for a path containing a lone surrogate', () => {
    expect(remoteEditorUrl('vscode', 'h', '/home/u/bad\udcffname')).toBeNull()
  })

  it('returns null for an unknown or empty editor (cannot mint an arbitrary scheme)', () => {
    expect(remoteEditorUrl('emacs', 'h', '/x')).toBeNull()
    expect(remoteEditorUrl('', 'h', '/x')).toBeNull()
  })

  it('returns null when host or path is empty', () => {
    expect(remoteEditorUrl('vscode', '', '/x')).toBeNull()
    expect(remoteEditorUrl('vscode', 'h', '')).toBeNull()
  })

  it('rejects non-absolute paths (a relative path would graft onto the SSH host)', () => {
    // `prod` + `-backup/etc/config.json` must NOT become authority `prod-backup`.
    expect(remoteEditorUrl('vscode', 'prod', '-backup/etc/config.json')).toBeNull()
    expect(remoteEditorUrl('vscode', 'h', 'relative/path.txt')).toBeNull()
    expect(remoteEditorUrl('vscode', 'h', './x')).toBeNull()
    expect(remoteEditorUrl('vscode', 'h', '~/x')).toBeNull()
  })

  it('rejects dot segments (URL normalisation would pop the SSH authority segment)', () => {
    // encodeURIComponent('..') is '..', and the URL parser removes dot segments
    // before the editor reads `ssh-remote+<host>`, so each of these would
    // otherwise SSH to a host chosen by whoever wrote the path.
    expect(remoteEditorUrl('vscode', 'h', '/home/u/w/link/../../../../../ssh-remote+attacker.example/x')).toBeNull()
    expect(remoteEditorUrl('vscode', 'h', '/../etc/hosts')).toBeNull()
    expect(remoteEditorUrl('vscode', 'h', '/a/./b')).toBeNull()
    expect(remoteEditorUrl('vscode', 'h', '/a/b/..')).toBeNull()
    expect(remoteEditorUrl('vscode', 'h', '/..')).toBeNull()
  })

  it('still accepts dotted names that are not dot segments', () => {
    expect(remoteEditorUrl('vscode', 'h', '/a/.env/..b/.../c.d')).toBe(
      'vscode://vscode-remote/ssh-remote+h/a/.env/..b/.../c.d',
    )
  })

  it('keeps the host-and-root link and legal host characters intact', () => {
    // The re-parse guard must not reject a bare `/`, a `//` run, or any
    // character the host charset allows (`+`, `@`, `.`, `_`, `-`).
    expect(remoteEditorUrl('vscode', 'h', '/')).toBe('vscode://vscode-remote/ssh-remote+h/')
    expect(remoteEditorUrl('vscode', 'h', '/a//b')).toBe('vscode://vscode-remote/ssh-remote+h/a//b')
    expect(remoteEditorUrl('vscode', 'me@box+1_2.lan-x', '/x')).toBe(
      'vscode://vscode-remote/ssh-remote+me@box+1_2.lan-x/x',
    )
  })
})

// ── Item-row aria labels ─────────────────────────────────────────────────────
// The render/hide/open-click/copy-click behaviour of the item rows is already
// covered by the `describe('FilePathMenu')` suite above through the same public
// wrapper (FilePathMenuItems is a private building block with no other entry
// point), so those cases are not repeated here. Only the accessible-name
// assertion — which the suite above does not make — is kept.

describe('FilePathMenu aria labels', () => {
  const TEST_PATH = '/tmp/demo.html'

  it('each item has an accessible aria-label', async () => {
    renderWithProviders(
      <FilePathMenu filePath={TEST_PATH}>
        <span data-testid="ctx-trigger">file.txt</span>
      </FilePathMenu>,
    )

    rightClick(screen.getByTestId('ctx-trigger'))

    await waitFor(() => {
      expect(screen.getByLabelText('Open with default app')).toBeInTheDocument()
    })
    expect(screen.getByLabelText('Show in file manager')).toBeInTheDocument()
    expect(screen.getByLabelText('Copy path')).toBeInTheDocument()
  })
})
