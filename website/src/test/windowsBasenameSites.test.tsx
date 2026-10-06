/**
 * Issue #14581: every dashboard site that shows a gateway path's file name
 * derives it through `pathBasename` (utils/pathBasename.ts), so a Windows
 * gateway's `C:\…` path shows `report.md`, not the whole path, while a POSIX
 * path splits exactly as before.
 *
 * The behaviour cases mount the cheap sites through their real code; the
 * source scan pins the rest (MarkdownPanel, ToolCallLine, FilesHomePanel,
 * FolderPanel, DiffBlock) against the POSIX-only shapes coming back, since
 * each of those needs a full panel harness for a one-line derivation.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { render, renderHook, act } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import FoldableDiffBlock, { resetExpandedDiffFences } from '../components/FoldableDiffBlock'
import { FilePreviewStrip } from '../components/chat-input/FilePreviewStrip'
import { ImageViewer } from '../components/FileRenderers'
import { usePanelTabs, __resetPanelTabs } from '../hooks/usePanelTabs'
import { basename as explorerBasename, crumbLabel, parentChain } from '../apps/file-explorer/utils'
import { WINDOWS_SHAPED_PATH_RE } from '../utils/pathBasename'
import { isWindowsShapedPath, normalizeWindowsPath } from '../utils/fileTokens'

const WIN = 'C:\\Users\\u\\docs\\report.md'
const UNC = '\\\\host\\share\\docs\\report.md'
const POSIX_BACKSLASH = '/tmp/we\\ird.md'

describe('Windows gateway paths show their file name (#14581)', () => {
  beforeEach(() => { resetExpandedDiffFences(); __resetPanelTabs() })

  it.each([
    [WIN, 'report.md'],
    [UNC, 'report.md'],
    ['C:/Users/u/report.md', 'report.md'],
    [POSIX_BACKSLASH, 'we\\ird.md'],
    ['/repo/src/report.md', 'report.md'],
  ])('FoldableDiffBlock chip names %s as %s', (pathHint, name) => {
    const { container } = render(<FoldableDiffBlock code={'@@ -1 +1 @@\n-a\n+b'} complete pathHint={pathHint} />)
    const chip = container.querySelector<HTMLElement>('[data-testid="prose-diff-chip"]')!
    expect(chip.querySelector('.font-mono')?.textContent).toBe(name)
  })

  it.each([
    [WIN, 'report.md'],
    [POSIX_BACKSLASH, 'we\\ird.md'],
  ])('FilePreviewStrip file chip names %s as %s', (path, name) => {
    const { getByRole } = render(<FilePreviewStrip files={[path]} />)
    expect(getByRole('group', { name: path }).querySelector('span')?.textContent).toBe(name)
  })

  it('FilePreviewStrip image preview label uses the file name of a Windows path', () => {
    const { container } = render(<FilePreviewStrip files={['C:\\Users\\u\\shot.png']} />)
    const btn = container.querySelector<HTMLElement>('button[aria-label]')!
    expect(btn.getAttribute('aria-label')).toContain('shot.png')
    expect(btn.getAttribute('aria-label')).not.toContain('C:\\')
  })

  it.each([
    ['C:\\Users\\u\\shot.png', 'shot.png'],
    ['/tmp/we\\ird.png', 'we\\ird.png'],
  ])('ImageViewer alt for %s is %s', (filePath, alt) => {
    const { container } = render(<ImageViewer filePath={filePath} />)
    expect(container.querySelector('img')?.getAttribute('alt')).toBe(alt)
  })

  it.each([
    [WIN, 'report.md'],
    ['C:\\Users\\u\\docs\\', 'docs'],
    [POSIX_BACKSLASH, 'we\\ird.md'],
    ['/a/b/', 'b'],
  ])('usePanelTabs titles a tab for %s as %s', (path, title) => {
    const { result } = renderHook(() => usePanelTabs(null, []))
    act(() => result.current.openFolder(path, 'slot-a'))
    expect(result.current.tabs.at(-1)?.title).toBe(title)
  })

  it.each([
    [WIN, 'report.md'],
    ['C:\\Users\\u\\docs\\', 'docs'],
    [POSIX_BACKSLASH, 'we\\ird.md'],
    ['/a/b/', 'b'],
    ['/', ''],
  ])('file-explorer basename(%s) is %s', (p, name) => {
    expect(explorerBasename(p)).toBe(name)
  })

  // The sites listed on #14581 that are not mounted above. Each derived its
  // name with a `/`-only split; the shared helper is the one place that knows
  // when `\` separates. (composerDrop.ts, the Mochi drop site, is PR #10952's.)
  it.each([
    'components/MarkdownPanel.tsx',
    'components/FileRenderers.tsx',
    'components/chat-input/FilePreviewStrip.tsx',
    'pages/chat/ToolCallLine.tsx',
    'components/FoldableDiffBlock.tsx',
    'components/DiffBlock.tsx',
    'pages/chat/FilesHomePanel.tsx',
    'pages/chat/FolderPanel.tsx',
    'hooks/usePanelTabs.ts',
    'apps/file-explorer/utils.ts',
  ])('%s derives file names through pathBasename only', (rel) => {
    const src = readFileSync(resolve(__dirname, '..', rel), 'utf8')
    expect(src).toMatch(/import \{[^}]*\bpathBasename\b[^}]*\} from '[./]+\/utils\/pathBasename'/)
    expect(src).not.toMatch(/\.split\('\/'\)\.pop\(\)/)
    expect(src).not.toMatch(/\.lastIndexOf\('\/'\) \+ 1\)/)
  })
})

// Breadcrumb navigation is out of scope: these pin main's behaviour so the
// separator-aware basename cannot collapse a Windows root crumb to `repo`.
describe('file-explorer breadcrumbs are unchanged by #14581', () => {
  it.each([
    ['C:\\Users\\u\\repo', ['/', 'C:\\Users\\u\\repo']],
    ['/a/b/c', ['/', '/a', '/a/b', '/a/b/c']],
    ['/tmp/we\\ird', ['/', '/tmp', '/tmp/we\\ird']],
  ])('parentChain(%s)', (p, chain) => {
    expect(parentChain(p)).toEqual(chain)
  })

  it.each([
    ['C:\\Users\\u\\repo', 'C:\\Users\\u\\repo'],
    ['C:/Users', 'Users'],
    ['/a/b/', 'b'],
    ['/tmp/we\\ird', 'we\\ird'],
  ])('crumbLabel(%s) is %s', (p, label) => {
    expect(crumbLabel(p)).toBe(label)
  })

  it('PathBar labels crumbs with crumbLabel, not the separator-aware basename', () => {
    const src = readFileSync(resolve(__dirname, '../apps/file-explorer/PathBar.tsx'), 'utf8')
    expect(src).toContain('crumbLabel(s)')
    expect(src).not.toMatch(/\bbasename\(s\)/)
  })

  it('fileTokens uses the one Windows-shape regex', () => {
    expect(normalizeWindowsPath('C:\\a\\b')).toBe('C:/a/b')
    expect(isWindowsShapedPath('\\\\host\\s')).toBe(WINDOWS_SHAPED_PATH_RE.test('\\\\host\\s'))
    const src = readFileSync(resolve(__dirname, '../utils/fileTokens.ts'), 'utf8')
    expect(src).not.toContain('/^(?:[A-Za-z]:|')
  })
})
