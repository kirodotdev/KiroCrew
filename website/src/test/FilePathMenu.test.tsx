import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import FilePathMenu from '../components/FilePathMenu'

// ── Mocks ────────────────────────────────────────────────────────────────────

const brandingEnv = vi.hoisted(() => ({ directLocal: true }))
const platformEnv = vi.hoisted(() => ({ value: 'other' as 'other' | 'darwin' | 'windows' }))

vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: brandingEnv.directLocal }),
}))

// The reveal label is platform-aware (names Finder / File Explorer on the
// gateway's own OS). Drive it explicitly so the label assertions are stable
// regardless of the test host.
vi.mock('../hooks/useGatewayPlatform', () => ({
  useGatewayPlatform: () => platformEnv.value,
}))

vi.mock('../api/client', () => ({
  api: {
    revealPath: vi.fn().mockResolvedValue({ ok: true }),
  },
}))

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn(),
}))

import { api } from '../api/client'
import { copyToClipboard } from '../utils/clipboard'

// ── Helpers ──────────────────────────────────────────────────────────────────

beforeEach(() => {
  vi.clearAllMocks()
  brandingEnv.directLocal = true
  platformEnv.value = 'other'
})

afterEach(() => {
  brandingEnv.directLocal = true
  platformEnv.value = 'other'
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
