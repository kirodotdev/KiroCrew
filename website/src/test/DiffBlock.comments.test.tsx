import { describe, it, expect, afterEach, beforeAll } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import DiffBlock, { diffBlockId } from '../components/DiffBlock'
import { parseReviewPatch } from '../pierre/PierreImpl'
import { SlotProvider, ReviewSurfaceProvider } from '../providers/SlotContext'
import { addReviewComment, peekReviewComments, clearReviewComments } from '../store/reviewComments'

/* Pierre owns the diff rows: gutters, line numbers and the hover "+" gutter
 * utility all live inside its shadow root, so the CLICK path (gutter button →
 * onGutterUtilityClick) is not reachable from a light-DOM test and is covered
 * by manual browser verification instead. What IS testable here is everything
 * DiffBlock itself contributes in the light DOM (the annotation widgets under
 * commented lines: draft chips, the edit form) plus `parseReviewPatch` — the
 * ONE parse that produces both what Pierre renders and the `ReviewFileRef`
 * identity the review hooks receive. There is deliberately no second patch
 * parser to test against: refs and rendered files come from the same
 * enumeration, so path/index drift between them is impossible by
 * construction, and these tests pin the shapes that broke the old dual-parser
 * design (same-basename files, deleted files, header-shaped hunk content,
 * and pure-rename entries with no ---/+++ pair). */

beforeAll(() => import('../pierre/PierreImpl'))

const SLOT = 'diff-test-slot'

const DIFF = [
  '--- a/src/Thing.kt',
  '+++ b/src/Thing.kt',
  '@@ -1,3 +1,4 @@',
  ' package demo',
  '-object Thing {',
  '+class Thing(',
  '+  val dep: Dep,',
  ') {',
].join('\n')

function renderDiff() {
  return render(
    <SlotProvider slotId={SLOT}>
      <ReviewSurfaceProvider>
        <DiffBlock code={DIFF} complete />
      </ReviewSurfaceProvider>
    </SlotProvider>,
  )
}

const BLOCK = () => diffBlockId(DIFF)

const refsOf = (patch: string) => parseReviewPatch(patch, 'test:patch').refs

afterEach(() => clearReviewComments(SLOT))

describe('parseReviewPatch file identity', () => {
  it('returns every file path in patch order, full paths preserved under display basenaming', () => {
    const multi = [
      '--- a/x/render.py', '+++ b/x/render.py', '@@ -1,1 +1,1 @@', '-a', '+b',
      '--- a/y/render.py', '+++ b/y/render.py', '@@ -1,1 +1,1 @@', '-c', '+d',
    ].join('\n')
    const { files, refs } = parseReviewPatch(multi, 'test:patch', true)
    expect(refs.map(r => r.path)).toEqual(['x/render.py', 'y/render.py'])
    // Display shows basenames; identity keeps the full paths apart.
    expect(files.map(f => f.name)).toEqual(['render.py', 'render.py'])
    expect(refs.map(r => r.index)).toEqual([0, 1])
  })

  it('identifies a deleted file (+++ /dev/null) by its old side', () => {
    const del = ['--- a/gone.ts', '+++ /dev/null', '@@ -1,1 +0,0 @@', '-bye'].join('\n')
    const refs = refsOf(del)
    expect(refs).toHaveLength(1)
    expect(refs[0].path).toBe('gone.ts')
  })

  it('keeps refs index-aligned with rendered files when a pure rename entry precedes same-basename files', () => {
    // A 100%-similarity rename carries NO ---/+++ header pair — the patch
    // shape that made every header-counting parser disagree with Pierre's
    // enumeration. Refs come from the same parse, so however Pierre counts
    // the rename entry, refs[i] describes exactly files[i].
    const withRename = [
      'diff --git a/lib/old.ts b/lib/moved.ts',
      'similarity index 100%',
      'rename from lib/old.ts',
      'rename to lib/moved.ts',
      'diff --git a/x/render.py b/x/render.py',
      '--- a/x/render.py',
      '+++ b/x/render.py',
      '@@ -1,1 +1,1 @@',
      '-a',
      '+b',
      'diff --git a/y/render.py b/y/render.py',
      '--- a/y/render.py',
      '+++ b/y/render.py',
      '@@ -1,1 +1,1 @@',
      '-c',
      '+d',
    ].join('\n')
    const { files, refs } = parseReviewPatch(withRename, 'test:patch')
    expect(refs).toHaveLength(files.length)
    refs.forEach((r, i) => expect(r.index).toBe(i))
    // The two same-basename files resolve to their own distinct full paths,
    // wherever the rename entry landed in the enumeration.
    const pyPaths = refs.map(r => r.path).filter(p => p.endsWith('render.py'))
    expect(pyPaths).toEqual(['x/render.py', 'y/render.py'])
    // And each maps to ITS file entry's content, not its neighbour's.
    const xRef = refs.find(r => r.path === 'x/render.py')!
    const yRef = refs.find(r => r.path === 'y/render.py')!
    expect(xRef.lineTextAt('new', 1)).toBe('b')
    expect(yRef.lineTextAt('new', 1)).toBe('d')
  })
})

describe('ReviewFileRef.lineTextAt', () => {
  it('resolves added, deleted and context lines', () => {
    const [ref] = refsOf(DIFF)
    expect(ref.lineTextAt('new', 2)).toBe('class Thing(')
    expect(ref.lineTextAt('new', 3)).toBe('  val dep: Dep,')
    expect(ref.lineTextAt('old', 2)).toBe('object Thing {')
    expect(ref.lineTextAt('new', 1)).toBe('package demo')
    expect(ref.lineTextAt('old', 1)).toBe('package demo')
  })

  it('returns null for a line outside the hunks', () => {
    const [ref] = refsOf(DIFF)
    expect(ref.lineTextAt('new', 99)).toBeNull()
  })

  it('scopes per file in a multi-file patch', () => {
    const multi = [
      '--- a/x.ts',
      '+++ b/x.ts',
      '@@ -1,1 +1,1 @@',
      '-const a = 1',
      '+const a = 2',
      '--- a/y.ts',
      '+++ b/y.ts',
      '@@ -1,1 +1,1 @@',
      '-const b = 1',
      '+const b = 9',
    ].join('\n')
    const refs = refsOf(multi)
    expect(refs[0].lineTextAt('new', 1)).toBe('const a = 2')
    expect(refs[1].lineTextAt('new', 1)).toBe('const b = 9')
  })

  it('produces no ref for a headerless hunk-only patch (surface falls back to plain text)', () => {
    // parsePatchFiles yields no file entry for bare hunks, and the render
    // path's zero-files guard sends the whole block to PlainCodeFallback —
    // no gutter, no drafts, so no identity is needed. Pinned so a Pierre
    // upgrade that starts parsing bare hunks shows up as a test delta.
    const bare = ['@@ -1,1 +1,2 @@', ' keep', '+added'].join('\n')
    expect(refsOf(bare)).toHaveLength(0)
  })

  it('does not mistake hunk content for a file header (-- x replaced by ++ y)', () => {
    // The body deletes a line reading `-- old` and adds `++ new`: those body
    // lines start with `--- ` / `+++ ` and sit adjacent, exactly the header
    // shape. Pierre's counted hunk parsing keeps them as content.
    const tricky = [
      '--- a/notes.md',
      '+++ b/notes.md',
      '@@ -1,2 +1,2 @@',
      '--- old marker',
      '+++ new marker',
      ' tail',
    ].join('\n')
    const refs = refsOf(tricky)
    expect(refs.map(r => r.path)).toEqual(['notes.md'])
    expect(refs[0].lineTextAt('old', 1)).toBe('-- old marker')
    expect(refs[0].lineTextAt('new', 1)).toBe('++ new marker')
  })

  it('resolves old-side lines of a deleted file (+++ /dev/null)', () => {
    const del = ['--- a/gone.ts', '+++ /dev/null', '@@ -1,2 +0,0 @@', '-first', '-second'].join('\n')
    const [ref] = refsOf(del)
    expect(ref.lineTextAt('old', 1)).toBe('first')
    expect(ref.lineTextAt('old', 2)).toBe('second')
  })

  it('gives distinct block ids to distinct diff content', () => {
    expect(diffBlockId('a')).not.toBe(diffBlockId('b'))
    expect(diffBlockId(DIFF)).toBe(diffBlockId(DIFF))
    // Same length, same djb2-vulnerable shape (the reviewer's collision
    // class): the double hash keeps them apart.
    expect(diffBlockId('old/new-000r')).not.toBe(diffBlockId('old/new-0020'))
  })
})

describe('DiffBlock review-comment annotations', () => {
  it('renders a pending draft as a chip under its line', async () => {
    addReviewComment(SLOT, { blockId: BLOCK(), fileIndex: 0, file: 'src/Thing.kt', side: 'new', line: 2, lineText: 'class Thing(', text: 'prefer an interface here' })
    renderDiff()
    expect(await screen.findByText('prefer an interface here')).toBeInTheDocument()
  })

  it('chip click opens the form prefilled and saving edits in place', async () => {
    addReviewComment(SLOT, { blockId: BLOCK(), fileIndex: 0, file: 'src/Thing.kt', side: 'new', line: 2, lineText: 'class Thing(', text: 'first wording' })
    renderDiff()
    fireEvent.click(await screen.findByText('first wording'))
    const box = await screen.findByPlaceholderText('Comment for the agent…') as HTMLTextAreaElement
    expect(box.value).toBe('first wording')
    fireEvent.change(box, { target: { value: 'better wording' } })
    // Text query rather than role: until Pierre's warm swap "paints" (which
    // happy-dom's zero-layout world never reports), the widgets live inside
    // WarmSwap's aria-hidden staging box, where roles are not exposed.
    fireEvent.click(screen.getByText('Save comment'))

    const drafts = peekReviewComments(SLOT)
    expect(drafts).toHaveLength(1)
    expect(drafts[0].text).toBe('better wording')
    expect(drafts[0].file).toBe('src/Thing.kt')
    expect(drafts[0].line).toBe(2)
    // The form closes after saving.
    expect(screen.queryByPlaceholderText('Comment for the agent…')).toBeNull()
  })

  it('escape closes the edit form without changing the draft', async () => {
    addReviewComment(SLOT, { blockId: BLOCK(), fileIndex: 0, file: 'src/Thing.kt', side: 'old', line: 2, lineText: 'object Thing {', text: 'why an object?' })
    renderDiff()
    fireEvent.click(await screen.findByText('why an object?'))
    const box = await screen.findByPlaceholderText('Comment for the agent…')
    fireEvent.keyDown(box, { key: 'Escape' })
    expect(screen.queryByPlaceholderText('Comment for the agent…')).toBeNull()
    expect(peekReviewComments(SLOT)[0].text).toBe('why an object?')
  })

  it('renders no draft chip outside a review surface (pane without a draining composer)', async () => {
    addReviewComment(SLOT, { blockId: BLOCK(), fileIndex: 0, file: 'src/Thing.kt', side: 'new', line: 2, lineText: 'class Thing(', text: 'invisible here' })
    // SlotProvider alone — no ReviewSurfaceProvider — models a split-view
    // pane whose composer never drains drafts: the gutter and chips are off.
    render(
      <SlotProvider slotId={SLOT}>
        <DiffBlock code={DIFF} complete />
      </SlotProvider>,
    )
    await screen.findByTitle('Copy patch')
    expect(screen.queryByText('invisible here')).toBeNull()
  })

  it('the chip remove button deletes the draft', async () => {
    addReviewComment(SLOT, { blockId: BLOCK(), fileIndex: 0, file: 'src/Thing.kt', side: 'new', line: 3, lineText: '  val dep: Dep,', text: 'inject lazily' })
    renderDiff()
    await screen.findByText('inject lazily')
    // Title query for the same aria-hidden staging-box reason as above.
    fireEvent.click(screen.getByTitle('Remove comment'))
    expect(peekReviewComments(SLOT)).toHaveLength(0)
  })
})
