import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

const read = (p: string) => readFile(join(__dirname, '..', p), 'utf8')

// `DetailPanel` sets its own pixel width with a `minWidth` floor applied AFTER
// every cap, so no caller can configure below it. Dropping that width is only
// half a fix: a caller that wraps the panel in its OWN content-sized box (an
// animated `width: 'auto'` with `shrink-0`) then hands it a box that hugs its
// content, and the panel comes out NARROWER than the floor it replaced.
//
// Measured in isolation at a 390px row:
//   before (pixel + 300px floor)      panel 380px, task view  10px
//   panel only, caller untouched      panel  71px  <-- collapses
//   caller only, panel still pixel    panel clipped to 195px
//   both + content steps aside        panel 390px  <-- correct
describe('DetailPanel at phone widths', () => {
  it('takes the existing full-width path instead of a pixel width', async () => {
    const s = await read('components/DetailPanel.tsx')
    expect(s, 'expected the viewport hook').toContain('useIsMobile')
    expect(s, 'the narrow branch must reuse the embedded early return')
      .toMatch(/if \(embedded \|\| isMobile\) \{/)
    // The floor is the documented root cause: lowering it cannot work, because
    // `clampPanelWidth` applies `minWidth` last.
    expect(s).toMatch(/Math\.max\(minWidth, Math\.min\(w, maxPanelWidth\(rowWidth, reserveWidth\)\)\)/)
  })

  it('is not left to fend for itself: the one caller with its own width wrapper moves too', async () => {
    const s = await read('pages/ProjectDetailPage.tsx')
    expect(s, 'expected the viewport hook').toContain('useIsMobile')
    expect(s, 'the animated wrapper width must move')
      .toMatch(/animate=\{\{ width: isMobile \? '100%' : 'auto', opacity: 1 \}\}/)
    expect(s, 'the wrapper must stop hugging its content while narrow')
      .toMatch(/\$\{isMobile \? 'flex-1 min-w-0' : 'shrink-0'\}/)
  })

  it('steps the task view aside so the panel owns the pane', async () => {
    const s = await read('pages/ProjectDetailPage.tsx')
    expect(s).toMatch(/\$\{isMobile && selected \? 'hidden' : ''\}/)
    // Hidden, not unmounted: the view holds scroll position and the DAG layout,
    // and rotating a phone crosses the breakpoint.
    expect(s, 'the task view must not be unmounted by a responsive branch')
      .not.toMatch(/\{!\(isMobile && selected\) && \(/)
  })

  it('no other caller wraps the panel in its own width box', async () => {
    // The two-sided fix is only safe because the caller set is small and known.
    // If another caller grows a width wrapper, this pins that it must be handled.
    const files = [
      'components/ArtifactPanel.tsx', 'components/MarkdownPanel.tsx',
      'components/GitPanel.tsx', 'pages/chat/FolderPanel.tsx',
      'pages/chat/SidePanel.tsx', 'pages/aidlc/TaskDetailPanel.tsx',
    ]
    for (const f of files) {
      const s = await read(f)
      const own = /animate=\{\{[^}]*width:\s*'auto'/.test(s)
      expect(own, `${f} grew its own animated width wrapper -- it needs the narrow branch too`).toBe(false)
    }
  })
})
