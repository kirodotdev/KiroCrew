/**
 * The file-path menu's in-app rows: "Open in side panel" and "Show folder in
 * side panel".
 *
 * A browser on another machine than the gateway cannot use the native rows
 * (they act on the gateway host), and an `ssh -L` viewer is indistinguishable
 * from a local one, so `directLocal` cannot be trusted to hide them. The in-app
 * rows are the menu's guaranteed working action: they render whenever the host
 * passes its handlers, in every deployment, and leave every native gate as it
 * is.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { stubPathKinds } from './pathKindStub'
import FilePathMenu from '../components/FilePathMenu'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { __resetPathKindCache } from '../hooks/usePathKind'
import { i18nT } from '../i18n/t'

// ── Mocks ────────────────────────────────────────────────────────────────────

const brandingEnv = vi.hoisted(() => ({ directLocal: false }))
const platformEnv = vi.hoisted(() => ({ value: 'other' as 'other' | 'darwin' | 'windows' }))

vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: brandingEnv.directLocal }),
}))

vi.mock('../hooks/useGatewayPlatform', () => ({
  useGatewayPlatform: () => platformEnv.value,
}))

vi.mock('../lib/electron', () => ({
  canOpenFileInEditor: () => false,
  openFileInEditor: vi.fn(),
}))

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, revealPath: vi.fn().mockResolvedValue({ ok: true }) } }
})

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
}))

import { api } from '../api/client'

// ── Helpers ──────────────────────────────────────────────────────────────────

const FILE = '/home/user/project/docs/report.md'
const DIR = '/home/user/project/docs'
const PARENT_OF_FILE = '/home/user/project/docs'

const OPEN_IN_PANEL = () => i18nT('components.filePathMenu.open_in_side_panel')
const OPEN_FOLDER = () => i18nT('components.filePathMenu.show_folder_in_side_panel')

const realFetch = globalThis.fetch

beforeEach(() => {
  vi.clearAllMocks()
  brandingEnv.directLocal = false
  platformEnv.value = 'other'
  __resetPathKindCache()
})

afterEach(() => {
  globalThis.fetch = realFetch
})

async function openMenu(ui: React.ReactElement) {
  renderWithProviders(ui)
  fireEvent.contextMenu(screen.getByTestId('trigger'))
  return waitFor(() => {
    const menu = document.querySelector('[role="menu"]')
    expect(menu).not.toBeNull()
    return menu as HTMLElement
  })
}

function item(label: string): HTMLElement {
  const el = Array.from(document.querySelectorAll('[role="menuitem"]'))
    .find(n => n.textContent?.includes(label))
  expect(el).toBeDefined()
  return el as HTMLElement
}

// ── FilePathMenu ─────────────────────────────────────────────────────────────

describe('FilePathMenu in-app rows', () => {
  it('offers both in-app rows for a file on a remote session, where the native rows are hidden', async () => {
    const menu = await openMenu(
      <FilePathMenu filePath={FILE} onFileOpen={vi.fn()} onFolderOpen={vi.fn()}>
        <span data-testid="trigger">report.md</span>
      </FilePathMenu>,
    )
    expect(menu.textContent).toContain(OPEN_IN_PANEL())
    expect(menu.textContent).toContain(OPEN_FOLDER())
    expect(menu.textContent).toContain('Copy path')
    expect(menu.textContent).not.toContain('Open with default app')
    expect(menu.textContent).not.toContain('Show in file manager')
  })

  it('"Open in side panel" hands the file to onFileOpen; the folder row hands its folder to onFolderOpen', async () => {
    const onFileOpen = vi.fn()
    const onFolderOpen = vi.fn()
    await openMenu(
      <FilePathMenu filePath={FILE} onFileOpen={onFileOpen} onFolderOpen={onFolderOpen}>
        <span data-testid="trigger">report.md</span>
      </FilePathMenu>,
    )
    fireEvent.click(item(OPEN_IN_PANEL()))
    expect(onFileOpen).toHaveBeenCalledWith(FILE)
    expect(onFolderOpen).not.toHaveBeenCalled()

    fireEvent.contextMenu(screen.getByTestId('trigger'))
    await waitFor(() => expect(document.querySelector('[role="menu"]')).not.toBeNull())
    fireEvent.click(item(OPEN_FOLDER()))
    expect(onFolderOpen).toHaveBeenCalledWith(PARENT_OF_FILE)
    // Neither in-app row touches the gateway's native opener.
    expect(api.revealPath).not.toHaveBeenCalled()
  })

  it('offers "Open in side panel" for a directory, opening that directory itself as a folder tab', async () => {
    const onFileOpen = vi.fn()
    const onFolderOpen = vi.fn()
    const menu = await openMenu(
      <FilePathMenu filePath={DIR} kind="dir" onFileOpen={onFileOpen} onFolderOpen={onFolderOpen}>
        <span data-testid="trigger">docs</span>
      </FilePathMenu>,
    )
    // One in-app row, and it names the directory itself: a parent-folder row
    // beside it would leave the reader unsure which of the two folders opens.
    expect(menu.textContent).not.toContain(OPEN_FOLDER())
    fireEvent.click(item(OPEN_IN_PANEL()))
    expect(onFolderOpen).toHaveBeenCalledWith(DIR)
    expect(onFileOpen).not.toHaveBeenCalled()
  })

  it('offers no in-app row for a directory when the host passes only a file opener', async () => {
    const menu = await openMenu(
      <FilePathMenu filePath={DIR} kind="dir" onFileOpen={vi.fn()}>
        <span data-testid="trigger">docs</span>
      </FilePathMenu>,
    )
    expect(menu.textContent).not.toContain(OPEN_IN_PANEL())
    expect(menu.textContent).not.toContain(OPEN_FOLDER())
  })

  it('opens the containing folder of a Windows gateway path with its own separator', async () => {
    const onFolderOpen = vi.fn()
    await openMenu(
      <FilePathMenu filePath={'C:\\Users\\me\\report.md'} onFolderOpen={onFolderOpen}>
        <span data-testid="trigger">report.md</span>
      </FilePathMenu>,
    )
    fireEvent.click(item(OPEN_FOLDER()))
    expect(onFolderOpen).toHaveBeenCalledWith('C:\\Users\\me')
  })

  it('offers no folder row for a path with no containing folder', async () => {
    const menu = await openMenu(
      <FilePathMenu filePath="report.md" onFolderOpen={vi.fn()}>
        <span data-testid="trigger">report.md</span>
      </FilePathMenu>,
    )
    expect(menu.textContent).not.toContain(OPEN_FOLDER())
    expect(menu.textContent).toContain('Copy path')
  })

  it('renders each in-app row only for the handler the host passed', async () => {
    const menu = await openMenu(
      <FilePathMenu filePath={FILE} onFileOpen={vi.fn()}>
        <span data-testid="trigger">report.md</span>
      </FilePathMenu>,
    )
    expect(menu.textContent).toContain(OPEN_IN_PANEL())
    expect(menu.textContent).not.toContain(OPEN_FOLDER())
  })

  it('adds no in-app rows when the host passes no handlers', async () => {
    const menu = await openMenu(
      <FilePathMenu filePath={FILE}>
        <span data-testid="trigger">report.md</span>
      </FilePathMenu>,
    )
    expect(menu.textContent).not.toContain(OPEN_IN_PANEL())
    expect(menu.textContent).not.toContain(OPEN_FOLDER())
    expect(menu.textContent).toContain('Copy path')
    // No in-app group, so no separator for one.
    expect(menu.querySelector('[role="separator"]')).toBeNull()
  })

  describe('native rows keep their own gates', () => {
    it('a direct-local session shows the native rows beside the in-app rows', async () => {
      brandingEnv.directLocal = true
      const menu = await openMenu(
        <FilePathMenu filePath={FILE} onFileOpen={vi.fn()} onFolderOpen={vi.fn()}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )
      expect(menu.textContent).toContain(OPEN_IN_PANEL())
      expect(menu.textContent).toContain(OPEN_FOLDER())
      expect(menu.textContent).toContain('Open with default app')
      expect(menu.textContent).toContain('Show in file manager')
      // The in-app rows are their own group, ahead of the native rows.
      const children = Array.from(menu.children)
      const separator = children.findIndex(el => el.getAttribute('role') === 'separator')
      const nativeOpen = children.findIndex(el => el.textContent?.includes('Open with default app'))
      const inAppFolder = children.findIndex(el => el.textContent?.includes(OPEN_FOLDER()))
      expect(inAppFolder).toBeGreaterThanOrEqual(0)
      expect(separator).toBeGreaterThan(inAppFolder)
      expect(nativeOpen).toBeGreaterThan(separator)
      fireEvent.click(item('Show in file manager'))
      await waitFor(() => expect(api.revealPath).toHaveBeenCalledWith(FILE, 'reveal'))
    })

    it('a Windows gateway still hides "Open with default app" when in-app handlers are passed', async () => {
      brandingEnv.directLocal = true
      platformEnv.value = 'windows'
      const menu = await openMenu(
        <FilePathMenu filePath={FILE} onFileOpen={vi.fn()} onFolderOpen={vi.fn()}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )
      expect(menu.textContent).toContain(OPEN_IN_PANEL())
      expect(menu.textContent).not.toContain('Open with default app')
      expect(menu.textContent).toContain('Open in File Explorer')
    })
  })
})

// ── Path chips (InlineCode host) ─────────────────────────────────────────────

/** Answer the batched kind probe (`POST /api/file-kinds`) with `kind` for every
 *  path. A `path:12` citation probes the literal spelling too; only the split
 *  path exists, so the chip resolves to the file plus its line. */
function stubKind(kind: 'file' | 'dir') {
  stubPathKinds(asked => (/:\d+$/.test(asked) ? null : kind))
}

async function rightClickChip(container: HTMLElement, kind: 'file' | 'dir') {
  const chip = await waitFor(() => {
    const c = container.querySelector(`code[data-path-kind="${kind}"]`)
    expect(c).not.toBeNull()
    return c!
  })
  fireEvent.contextMenu(chip)
  return waitFor(() => {
    const menu = document.querySelector('[role="menu"]')
    expect(menu).not.toBeNull()
    return menu as HTMLElement
  })
}

describe('path chip menu threads the renderer handlers', () => {
  it('a remote file chip opens itself in the side panel, keeping its cited line', async () => {
    stubKind('file')
    const onFileOpen = vi.fn()
    const onFolderOpen = vi.fn()
    const { container } = render(
      <MarkdownRenderer content={`\`${FILE}:12\``} onFileOpen={onFileOpen} onFolderOpen={onFolderOpen} />,
    )
    const menu = await rightClickChip(container, 'file')
    expect(menu.textContent).toContain(OPEN_IN_PANEL())
    expect(menu.textContent).not.toContain('Open with default app')

    fireEvent.click(item(OPEN_IN_PANEL()))
    expect(onFileOpen).toHaveBeenCalledWith(FILE, { line: 12 })
  })

  it('a remote file chip opens its containing folder in the side panel', async () => {
    stubKind('file')
    const onFolderOpen = vi.fn()
    const { container } = render(
      <MarkdownRenderer content={`\`${FILE}\``} onFileOpen={vi.fn()} onFolderOpen={onFolderOpen} />,
    )
    await rightClickChip(container, 'file')
    fireEvent.click(item(OPEN_FOLDER()))
    expect(onFolderOpen).toHaveBeenCalledWith(PARENT_OF_FILE)
  })

  it('a remote directory chip opens that directory in the side panel', async () => {
    stubKind('dir')
    const onFolderOpen = vi.fn()
    const { container } = render(
      <MarkdownRenderer content={`\`${DIR}\``} onFileOpen={vi.fn()} onFolderOpen={onFolderOpen} />,
    )
    const menu = await rightClickChip(container, 'dir')
    expect(menu.textContent).not.toContain(OPEN_FOLDER())
    fireEvent.click(item(OPEN_IN_PANEL()))
    expect(onFolderOpen).toHaveBeenCalledWith(DIR)
  })

  it('a chip in a renderer with no handlers keeps the native-only menu', async () => {
    stubKind('file')
    const { container } = render(<MarkdownRenderer content={`\`${FILE}\``} />)
    const menu = await rightClickChip(container, 'file')
    expect(menu.textContent).not.toContain(OPEN_IN_PANEL())
    expect(menu.textContent).not.toContain(OPEN_FOLDER())
    expect(menu.textContent).toContain('Copy path')
  })
})
