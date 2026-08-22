import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { __resetPathKindCache } from '../hooks/usePathKind'

// ── Mocks ────────────────────────────────────────────────────────────────────

const brandingEnv = vi.hoisted(() => ({ directLocal: true }))

vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: brandingEnv.directLocal }),
}))

vi.mock('../api/client', () => ({
  api: {
    revealPath: vi.fn().mockResolvedValue({ ok: true }),
  },
  // `revealOrOpen` (the shared helper the chip now routes through) references
  // `ApiError` in its failure branch, so the whole-module mock must export it.
  ApiError: class ApiError extends Error {
    status: number
    authRequired: boolean
    constructor(status = 0, message = '', _body = '', authRequired = false) {
      super(message)
      this.status = status
      this.authRequired = authRequired
    }
  },
}))

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn(),
}))

import { api } from '../api/client'
import { copyToClipboard } from '../utils/clipboard'

// ── Helpers ──────────────────────────────────────────────────────────────────

const realFetch = globalThis.fetch

function stubKind(kind: 'file' | 'dir', ok = true) {
  const headers = new Headers(kind ? { 'X-Path-Kind': kind } : {})
  globalThis.fetch = vi.fn(() =>
    Promise.resolve({ ok, status: ok ? 200 : 404, headers } as Response),
  ) as unknown as typeof fetch
}

function rightClick(el: Element) {
  fireEvent.contextMenu(el)
}

beforeEach(() => {
  vi.clearAllMocks()
  brandingEnv.directLocal = true
  __resetPathKindCache()
})

afterEach(() => {
  globalThis.fetch = realFetch
  vi.restoreAllMocks()
})

// ── Right-click context menu on file chips ───────────────────────────────────

describe('MarkdownRenderer path chips — context menu (FilePathMenu)', () => {
  const TEST_PATH = '/home/user/project/main.py'

  it('right-click on a confirmed file chip opens the context menu', async () => {
    stubKind('file')
    const { container } = render(
      <MarkdownRenderer content={`\`${TEST_PATH}\``} />,
    )

    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind="file"]')
      expect(c).not.toBeNull()
      return c!
    })

    rightClick(chip)

    await waitFor(() => {
      expect(document.querySelector('[role="menu"]')).not.toBeNull()
    })
  })

  it('context menu shows all three items when directLocal is true', async () => {
    stubKind('file')
    const { container } = render(
      <MarkdownRenderer content={`\`${TEST_PATH}\``} />,
    )

    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind="file"]')
      expect(c).not.toBeNull()
      return c!
    })

    rightClick(chip)

    await waitFor(() => {
      const menu = document.querySelector('[role="menu"]')
      expect(menu).not.toBeNull()
      expect(menu!.textContent).toContain('Open with default app')
      expect(menu!.textContent).toContain('Show in file manager')
      expect(menu!.textContent).toContain('Copy path')
    })
  })

  it('"Open with default app" calls revealPath with action "open"', async () => {
    stubKind('file')
    const { container } = render(
      <MarkdownRenderer content={`\`${TEST_PATH}\``} />,
    )

    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind="file"]')
      expect(c).not.toBeNull()
      return c!
    })

    rightClick(chip)

    await waitFor(() => {
      expect(document.querySelector('[role="menu"]')).not.toBeNull()
    })

    const items = document.querySelectorAll('[role="menuitem"]')
    const openItem = Array.from(items).find(el => el.textContent?.includes('Open with default app'))
    expect(openItem).toBeDefined()
    fireEvent.click(openItem!)

    await waitFor(() => {
      expect(api.revealPath).toHaveBeenCalledWith(TEST_PATH, 'open')
    })
  })

  it('"Show in file manager" calls revealPath with action "reveal"', async () => {
    stubKind('file')
    const { container } = render(
      <MarkdownRenderer content={`\`${TEST_PATH}\``} />,
    )

    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind="file"]')
      expect(c).not.toBeNull()
      return c!
    })

    rightClick(chip)

    await waitFor(() => {
      expect(document.querySelector('[role="menu"]')).not.toBeNull()
    })

    const items = document.querySelectorAll('[role="menuitem"]')
    const revealItem = Array.from(items).find(el => el.textContent?.includes('Show in file manager'))
    expect(revealItem).toBeDefined()
    fireEvent.click(revealItem!)

    await waitFor(() => {
      expect(api.revealPath).toHaveBeenCalledWith(TEST_PATH, 'reveal')
    })
  })

  it('"Copy path" calls copyToClipboard with the chip path', async () => {
    stubKind('file')
    const { container } = render(
      <MarkdownRenderer content={`\`${TEST_PATH}\``} />,
    )

    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind="file"]')
      expect(c).not.toBeNull()
      return c!
    })

    rightClick(chip)

    await waitFor(() => {
      expect(document.querySelector('[role="menu"]')).not.toBeNull()
    })

    const items = document.querySelectorAll('[role="menuitem"]')
    const copyItem = Array.from(items).find(el => el.textContent?.includes('Copy path'))
    expect(copyItem).toBeDefined()
    fireEvent.click(copyItem!)

    expect(copyToClipboard).toHaveBeenCalledWith(TEST_PATH)
  })

  it('hides open/reveal when directLocal is false (remote session)', async () => {
    brandingEnv.directLocal = false
    stubKind('file')
    const { container } = render(
      <MarkdownRenderer content={`\`${TEST_PATH}\``} />,
    )

    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind="file"]')
      expect(c).not.toBeNull()
      return c!
    })

    rightClick(chip)

    await waitFor(() => {
      const menu = document.querySelector('[role="menu"]')
      expect(menu).not.toBeNull()
      expect(menu!.textContent).toContain('Copy path')
      expect(menu!.textContent).not.toContain('Open with default app')
      expect(menu!.textContent).not.toContain('Show in file manager')
    })
  })

  it('chip tooltip promises reveal when directLocal is true', async () => {
    stubKind('file')
    const { container } = render(
      <MarkdownRenderer content={`\`${TEST_PATH}\``} />,
    )
    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind="file"]') as HTMLElement | null
      expect(c).not.toBeNull()
      return c!
    })
    // The gateway platform is unseeded (generic arm), so the shift+click clause
    // names the generic file manager rather than Finder/Explorer.
    expect(chip.getAttribute('title')).toContain('Shift+click to show in file manager')
    expect(chip.getAttribute('title')).not.toContain('copy path')
  })

  it('chip tooltip promises a path copy — not a reveal — when directLocal is false', async () => {
    // Item 2: on a remote session /api/reveal degrades shift+click to a
    // clipboard copy, so the hover must stop promising a Finder/Explorer reveal.
    brandingEnv.directLocal = false
    stubKind('file')
    const { container } = render(
      <MarkdownRenderer content={`\`${TEST_PATH}\``} />,
    )
    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind="file"]') as HTMLElement | null
      expect(c).not.toBeNull()
      return c!
    })
    const title = chip.getAttribute('title') ?? ''
    expect(title).toContain('Shift+click to copy path')
    expect(title).not.toContain('reveal in Finder')
    expect(title).not.toContain('file manager')
  })

  it('right-click on a directory chip opens the menu WITHOUT "Open with default app"', async () => {
    stubKind('dir', false)
    const dirPath = '/Users/me/workspace/project'
    const { container } = render(
      <MarkdownRenderer content={`\`${dirPath}\``} />,
    )

    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind="dir"]')
      expect(c).not.toBeNull()
      return c!
    })

    rightClick(chip)

    await waitFor(() => {
      const menu = document.querySelector('[role="menu"]')
      expect(menu).not.toBeNull()
      // Reveal + copy still apply to a directory; only Open (which the reveal
      // endpoint 400s on a dir) is suppressed.
      expect(menu!.textContent).toContain('Show in file manager')
      expect(menu!.textContent).toContain('Copy path')
      expect(menu!.textContent).not.toContain('Open with default app')
    })
  })
})

// ── Shift-click behavior preserved ──────────────────────────────────────────

describe('MarkdownRenderer path chips — shift-click reveal preserved with context menu', () => {
  it('shift-click still reveals via revealPath (not the context menu)', async () => {
    stubKind('file')
    const onFileOpen = vi.fn()
    const reveal = vi.spyOn(api, 'revealPath').mockResolvedValue({ ok: true } as never)
    const { container } = render(
      <MarkdownRenderer content={'`/home/user/a.md`'} onFileOpen={onFileOpen} />,
    )

    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind]')
      expect(c).not.toBeNull()
      return c!
    })

    fireEvent.click(chip, { shiftKey: true })
    // Shift+click now routes through the shared `revealOrOpen` helper, which
    // calls `api.revealPath(path, 'reveal')` — still the transport call, now
    // with the explicit action argument the helper always passes.
    expect(reveal).toHaveBeenCalledWith('/home/user/a.md', 'reveal')
    expect(onFileOpen).not.toHaveBeenCalled()
  })

  it('plain click still routes to onFileOpen (not intercepted by menu)', async () => {
    stubKind('file')
    const onFileOpen = vi.fn()
    const { container } = render(
      <MarkdownRenderer content={'`/home/user/a.md`'} onFileOpen={onFileOpen} />,
    )

    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind]')
      expect(c).not.toBeNull()
      return c!
    })

    fireEvent.click(chip)
    expect(onFileOpen).toHaveBeenCalledWith('/home/user/a.md')
  })
})

// ── Keyboard accessibility ───────────────────────────────────────────────────

describe('MarkdownRenderer path chips — keyboard access with context menu', () => {
  it('chip remains keyboard-accessible: role=button, tabIndex=0, Enter activates', async () => {
    stubKind('file')
    const onFileOpen = vi.fn()
    const { container } = render(
      <MarkdownRenderer content={'`/home/user/a.md`'} onFileOpen={onFileOpen} />,
    )

    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind]') as HTMLElement | null
      expect(c).not.toBeNull()
      return c!
    })

    expect(chip.getAttribute('role')).toBe('button')
    expect(chip.tabIndex).toBe(0)
    fireEvent.keyDown(chip, { key: 'Enter' })
    expect(onFileOpen).toHaveBeenCalledWith('/home/user/a.md')
  })

  it('Space key also activates the chip', async () => {
    stubKind('file')
    const onFileOpen = vi.fn()
    const { container } = render(
      <MarkdownRenderer content={'`/home/user/a.md`'} onFileOpen={onFileOpen} />,
    )

    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind]') as HTMLElement | null
      expect(c).not.toBeNull()
      return c!
    })

    fireEvent.keyDown(chip, { key: ' ' })
    expect(onFileOpen).toHaveBeenCalledWith('/home/user/a.md')
  })
})
