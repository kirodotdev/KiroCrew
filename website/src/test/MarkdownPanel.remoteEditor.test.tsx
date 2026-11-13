import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import { OverflowMenu } from '../components/MarkdownPanel'
import FilePathMenu from '../components/FilePathMenu'
import { remoteEditorUrl } from '../utils/remoteEditorUrl'

/**
 * "Open in <editor>" in the Markdown panel's ⋯ menu: the gateway file the panel
 * shows, opened in the viewer's own editor over the viewer's own SSH, built from
 * Settings → Display → Remote editor (served through branding).
 */

vi.mock('../api/client', () => ({
  api: {
    artifacts: vi.fn().mockResolvedValue({ artifacts: [] }),
    artifact: vi.fn(),
    createArtifact: vi.fn(),
    revealPath: vi.fn(),
    listApps: vi.fn(),
    invokeFileMenuItem: vi.fn().mockResolvedValue({}),
  },
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
}))

const brandingEnv = vi.hoisted(() => ({ remoteEditor: 'vscode', remoteEditorHost: 'dev-box' }))
vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({
    botName: 'Test', avatar: '',
    // A gateway on another machine: no gateway open/reveal rows.
    directLocal: false,
    desktopLauncher: false,
    remoteEditor: brandingEnv.remoteEditor,
    remoteEditorHost: brandingEnv.remoteEditorHost,
  }),
}))

const paneEnv = vi.hoisted(() => ({ embedded: false, electron: false }))
vi.mock('../lib/embedded', async importOriginal => ({
  ...(await importOriginal<typeof import('../lib/embedded')>()),
  isEmbeddedPane: () => paneEnv.embedded,
}))
vi.mock('../lib/electron', async importOriginal => ({
  ...(await importOriginal<typeof import('../lib/electron')>()),
  get isElectron() { return paneEnv.electron },
}))

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter>
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  </MemoryRouter>
)

function openMenu(filePath: string) {
  render(<OverflowMenu onError={vi.fn()} filePath={filePath} content="x" />, { wrapper })
  fireEvent.click(screen.getByTestId('markdown-panel-more-options'))
}

beforeEach(() => {
  brandingEnv.remoteEditor = 'vscode'
  brandingEnv.remoteEditorHost = 'dev-box'
  paneEnv.embedded = false
  paneEnv.electron = false
  queryClient.clear()
})

describe('Markdown panel ⋯ menu: Open in VS Code', () => {
  it('links to the file on the SSH host, each path segment encoded', () => {
    openMenu('/home/u/my notes#1 (copy).md')
    const row = screen.getByRole('menuitem', { name: 'Open in VS Code' })
    expect(row.getAttribute('href')).toBe('vscode://vscode-remote/ssh-remote+dev-box/home/u/my%20notes%231%20%28copy%29.md')
    expect(row.getAttribute('target')).toBeNull()
  })

  it('is not offered while no editor is picked', () => {
    brandingEnv.remoteEditor = ''
    openMenu('/home/u/a.md')
    expect(screen.getByRole('menuitem', { name: 'Copy path' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Open in VS Code' })).toBeNull()
  })

  it('opens as a new window from an embedded pane, which refuses an in-frame editor link', () => {
    paneEnv.embedded = true
    openMenu('/home/u/a.md')
    expect(screen.getByRole('menuitem', { name: 'Open in VS Code' }).getAttribute('target')).toBe('_blank')
  })

  it('opens as a new window from a desktop-app window, whose navigation guard refuses an editor link', () => {
    // A remote gateway's dashboard is the window's own document there, not an embedded pane.
    paneEnv.electron = true
    openMenu('/home/u/a.md')
    expect(screen.getByRole('menuitem', { name: 'Open in VS Code' }).getAttribute('target')).toBe('_blank')
  })

  it('is not in a path chip menu, whose path may name a file on another machine', () => {
    render(
      <FilePathMenu filePath="/etc/hosts">
        <span data-testid="trigger">/etc/hosts</span>
      </FilePathMenu>,
      { wrapper },
    )
    fireEvent.contextMenu(screen.getByTestId('trigger'))
    expect(screen.getByRole('menuitem', { name: 'Copy path' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Open in VS Code' })).toBeNull()
  })
})

describe('remoteEditorUrl', () => {
  it('builds no link a dot segment could point at another host', () => {
    expect(remoteEditorUrl('vscode', 'dev-box', '/a/../../ssh-remote+other/x')).toBeNull()
    expect(remoteEditorUrl('vscode', 'dev-box', 'relative/x')).toBeNull()
    expect(remoteEditorUrl('vscode', 'dev-box', '/home/u/.env')).toBe('vscode://vscode-remote/ssh-remote+dev-box/home/u/.env')
  })

  it('builds no link for a name the desktop editor-link check refuses', () => {
    // electron/external-scheme.js editorLinkTarget drops a backslash or control character.
    expect(remoteEditorUrl('vscode', 'dev-box', '/home/u/we\\ird.md')).toBeNull()
    expect(remoteEditorUrl('vscode', 'dev-box', '/home/u/a\u0007.md')).toBeNull()
  })
})
