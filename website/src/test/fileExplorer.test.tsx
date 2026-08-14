/**
 * Tests for the file-explorer builtin app frontend.
 * Covers utils, hooks, api, FileViewer sensitive path detection,
 * and TreeNode rendering. Targets ≥60% new line coverage.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

// ── Utils tests ──

describe('file-explorer/utils', () => {
  let utils: typeof import('../apps/file-explorer/utils')

  beforeEach(async () => {
    utils = await import('../apps/file-explorer/utils')
  })

  describe('extOf', () => {
    it('returns extension for normal file', () => {
      expect(utils.extOf('/path/to/file.ts')).toBe('.ts')
    })
    it('returns empty for no extension', () => {
      expect(utils.extOf('/path/to/Makefile')).toBe('')
    })
    it('returns lowercase', () => {
      expect(utils.extOf('FILE.TSX')).toBe('.tsx')
    })
    it('handles dot in directory name', () => {
      expect(utils.extOf('/path.d/file')).toBe('')
    })
  })

  describe('basename', () => {
    it('returns filename', () => {
      expect(utils.basename('/path/to/file.txt')).toBe('file.txt')
    })
    it('handles trailing slash', () => {
      expect(utils.basename('/path/to/dir/')).toBe('dir')
    })
    it('handles root', () => {
      expect(utils.basename('/')).toBe('')
    })
  })

  describe('dirname', () => {
    it('returns parent directory', () => {
      expect(utils.dirname('/path/to/file.txt')).toBe('/path/to')
    })
    it('returns / for top-level file', () => {
      expect(utils.dirname('/file.txt')).toBe('/')
    })
  })

  describe('parentChain', () => {
    it('returns chain from root to path', () => {
      const chain = utils.parentChain('/a/b/c')
      expect(chain).toEqual(['/', '/a', '/a/b', '/a/b/c'])
    })
    it('handles root', () => {
      expect(utils.parentChain('/')).toEqual(['/'])
    })
  })

  describe('formatBytes', () => {
    // Delegates to the shared `fmtBytes`. Two deliberate deltas: the unit is
    // CLDR-narrow (no space, and `kB` is the SI spelling), and the divisor is
    // 1000 rather than 1024 so the SI label is honest — 2048 bytes really is
    // 2.0 kB, not "2.0 KB".
    it('formats bytes', () => {
      expect(utils.formatBytes(500)).toBe('500B')
    })
    it('formats KB', () => {
      expect(utils.formatBytes(2048)).toBe('2kB')
    })
    it('formats MB', () => {
      expect(utils.formatBytes(5 * 1024 * 1024)).toBe('5.2MB')
    })
    it('returns empty for null', () => {
      expect(utils.formatBytes(null)).toBe('')
    })
  })

  describe('formatTime', () => {
    it('returns empty for 0', () => {
      expect(utils.formatTime(0)).toBe('')
    })
    it('returns locale string for valid timestamp', () => {
      const result = utils.formatTime(1700000000)
      expect(result).toBeTruthy()
      expect(result.length).toBeGreaterThan(5)
    })
    it('still dates a pre-epoch mtime', () => {
      // `/api/files` forwards `st_mtime` raw, and a restored archive or a bad
      // clock can make that NEGATIVE. The i18n seam's `toDate` treats `<= 0` as
      // unparseable and returns an em dash, so `formatTime` converts the seconds
      // to a `Date` itself rather than passing the number through — without that,
      // a pre-epoch file silently lost its timestamp in the listing.
      const result = utils.formatTime(-1)
      expect(result).not.toBe('—')
      expect(result).toContain('1969')
    })
  })

  describe('isShortcut', () => {
    it('returns true for metaKey', () => {
      expect(utils.isShortcut({ metaKey: true, ctrlKey: false } as KeyboardEvent)).toBe(true)
    })
    it('returns true for ctrlKey', () => {
      expect(utils.isShortcut({ metaKey: false, ctrlKey: true } as KeyboardEvent)).toBe(true)
    })
    it('returns false for neither', () => {
      expect(utils.isShortcut({ metaKey: false, ctrlKey: false } as KeyboardEvent)).toBe(false)
    })
    it('returns false for both — Ctrl+Cmd is an OS-reserved chord', () => {
      expect(utils.isShortcut({ metaKey: true, ctrlKey: true } as KeyboardEvent)).toBe(false)
    })
  })

  describe('loadState / saveState', () => {
    beforeEach(() => {
      localStorage.clear()
    })

    it('returns null when nothing saved', () => {
      expect(utils.loadState()).toBeNull()
    })

    it('returns null for invalid JSON', () => {
      localStorage.setItem('kc:file-explorer:state:v2', 'not json')
      expect(utils.loadState()).toBeNull()
    })

    it('returns null when folderTabs is not array', () => {
      localStorage.setItem('kc:file-explorer:state:v2', JSON.stringify({ folderTabs: 'not array' }))
      expect(utils.loadState()).toBeNull()
    })

    it('roundtrips valid state', () => {
      const state = { folderTabs: [{ id: '1', rootPath: '/' }], fileTabs: [] }
      utils.saveState(state)
      const loaded = utils.loadState()
      expect(loaded).not.toBeNull()
      expect(loaded.folderTabs[0].id).toBe('1')
    })
  })
})

// ── Hooks tests ──

describe('file-explorer/hooks', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('useDebouncedValue returns initial value immediately', async () => {
    const { useDebouncedValue } = await import('../apps/file-explorer/hooks')
    const { result } = renderHook(() => useDebouncedValue('hello', 200))
    expect(result.current).toBe('hello')
  })

  it('useDebouncedValue debounces updates', async () => {
    const { useDebouncedValue } = await import('../apps/file-explorer/hooks')
    const { result, rerender } = renderHook(
      ({ value }) => useDebouncedValue(value, 200),
      { initialProps: { value: 'a' } }
    )
    rerender({ value: 'b' })
    expect(result.current).toBe('a') // not yet updated
    act(() => { vi.advanceTimersByTime(200) })
    expect(result.current).toBe('b') // now updated
  })
})

// ── API tests ──

describe('file-explorer/api', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('health calls correct URL with credentials', async () => {
    const mockResponse = { ok: true, json: () => Promise.resolve({ allowedRoots: ['/home'] }) }
    vi.mocked(fetch).mockResolvedValue(mockResponse as unknown as Response)

    const { fileExplorerApi } = await import('../apps/file-explorer/api')
    const result = await fileExplorerApi.health()
    expect(fetch).toHaveBeenCalledWith('/apps/file-explorer/api/health', { credentials: 'same-origin' })
    expect(result.allowedRoots).toEqual(['/home'])
  })

  it('throws on non-ok response', async () => {
    const mockResponse = { ok: false, status: 403, text: () => Promise.resolve('forbidden') }
    vi.mocked(fetch).mockResolvedValue(mockResponse as unknown as Response)

    const { fileExplorerApi } = await import('../apps/file-explorer/api')
    await expect(fileExplorerApi.health()).rejects.toThrow('forbidden')
  })

  it('tree encodes path parameter', async () => {
    const mockResponse = { ok: true, json: () => Promise.resolve({ entries: [] }) }
    vi.mocked(fetch).mockResolvedValue(mockResponse as unknown as Response)

    const { fileExplorerApi } = await import('../apps/file-explorer/api')
    await fileExplorerApi.tree('/path with spaces', 2)
    expect(fetch).toHaveBeenCalledWith(
      '/apps/file-explorer/api/tree?path=%2Fpath%20with%20spaces&depth=2',
      { credentials: 'same-origin' }
    )
  })

  it('search builds correct params', async () => {
    const mockResponse = { ok: true, json: () => Promise.resolve({ results: [], engine: 'rg' }) }
    vi.mocked(fetch).mockResolvedValue(mockResponse as unknown as Response)

    const { fileExplorerApi } = await import('../apps/file-explorer/api')
    await fileExplorerApi.search('/root', 'query', '*.ts')
    const url = vi.mocked(fetch).mock.calls[0][0] as string
    expect(url).toContain('q=query')
    expect(url).toContain('include=*.ts')
  })

  it('read includes max_bytes when provided', async () => {
    const mockResponse = { ok: true, json: () => Promise.resolve({ content: 'hi' }) }
    vi.mocked(fetch).mockResolvedValue(mockResponse as unknown as Response)

    const { fileExplorerApi } = await import('../apps/file-explorer/api')
    await fileExplorerApi.read('/file.txt', 1024)
    const url = vi.mocked(fetch).mock.calls[0][0] as string
    expect(url).toContain('max_bytes=1024')
  })
})

// ── Constants tests ──

describe('file-explorer/constants', () => {
  it('IMAGE_EXTS contains common formats', async () => {
    const { IMAGE_EXTS } = await import('../apps/file-explorer/constants')
    expect(IMAGE_EXTS.has('.png')).toBe(true)
    expect(IMAGE_EXTS.has('.jpg')).toBe(true)
    expect(IMAGE_EXTS.has('.svg')).toBe(true)
    expect(IMAGE_EXTS.has('.txt')).toBe(false)
  })

  it('LANG_BY_EXT maps typescript', async () => {
    const { LANG_BY_EXT } = await import('../apps/file-explorer/constants')
    expect(LANG_BY_EXT['.ts']).toBe('typescript')
    expect(LANG_BY_EXT['.py']).toBe('python')
  })
})

// ── FileViewer sensitive path detection (unit test without rendering heavy MarkdownRenderer) ──

describe('file-explorer/FileViewer sensitive detection', () => {
  it('detects .ssh paths', async () => {
    const { isSensitivePath } = await import('../apps/file-explorer/utils')
    expect(isSensitivePath('/home/user/.ssh/id_rsa')).toBe(true)
    expect(isSensitivePath('/home/user/.aws/credentials')).toBe(true)
    expect(isSensitivePath('/home/user/.gnupg/private-keys')).toBe(true)
    expect(isSensitivePath('/home/user/project/.env')).toBe(true)
    expect(isSensitivePath('/home/user/.npmrc')).toBe(true)
    expect(isSensitivePath('/home/user/.kube/config')).toBe(true)
  })

  it('does not flag regular files', async () => {
    const { isSensitivePath } = await import('../apps/file-explorer/utils')
    expect(isSensitivePath('/home/user/code.ts')).toBe(false)
    expect(isSensitivePath('/home/user/project/src/main.py')).toBe(false)
    expect(isSensitivePath('/tmp/test.txt')).toBe(false)
  })

})
