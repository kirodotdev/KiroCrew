/**
 * Codebase sessions: the model (scan decoding, the City layout, the folder graph,
 * what the conductor reads and changes) and the pane (root, pick, ask, a reply's
 * new root). The three.js scene itself needs WebGL, so the pane test swaps it for a
 * stub that exposes the same pick callback.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  api: { getDoc: vi.fn(), saveDoc: vi.fn(), codebaseScan: vi.fn(), resolveApproval: vi.fn(), artifact: vi.fn(() => Promise.reject(new Error('404'))) },
}))
vi.mock('../chat-core/transport/sendTurn', () => ({ sendTurn: vi.fn() }))
vi.mock('../pages/chat/page/codebase/CodebaseScene', () => ({
  default: ({ model, lens, onPick }: { model: { files: { path: string }[] }; lens: string; onPick: (id: string | null, add: boolean) => void }) => (
    <div data-testid="scene-stub" data-lens={lens} data-files={model.files.length}>
      {model.files.map(f => <button key={f.path} type="button" onClick={e => onPick(`file:${f.path}`, e.shiftKey)}>{f.path}</button>)}
      <button type="button" onClick={() => onPick(null, false)}>empty</button>
    </div>
  ),
}))

vi.mock('../components/ProjectPicker', () => ({
  default: ({ onSelect, startPath }: { onSelect: (p: string) => void; startPath?: string }) => (
    <div data-testid="picker-stub" data-start={startPath}><button type="button" onClick={() => onSelect('/picked/repo')}>pick /picked/repo</button></div>
  ),
}))

import { api, ApiError } from '../api/client'
import { sendTurn } from '../chat-core/transport/sendTurn'
import { store } from '../store'
import { replaceMessages, setActiveSlot } from '../store/chatSlice'
import CodebasePane, { changedPaths } from '../pages/chat/page/codebase/CodebasePane'
import {
  CODEBASE_ID_PREFIX, decodeScan, describeNodes, focusIn, graphOf, isCodebaseId, layoutCity, parseCodebaseDoc,
  recencyColors, rootIn, serializeCodebaseDoc, squarify, stepGraph, summarizeCodebase, type CodebaseScanResponse,
} from '../pages/chat/page/codebase/codebaseModel'
import { buildCodebaseTurn, codebaseChangesFromMessages } from '../pages/chat/page/codebase/codebaseTurn'
import { splitReply } from '../pages/chat/page/docConductor'
import type { ChatMessage } from '../types'

const SCAN: CodebaseScanResponse = {
  sig: 's1', root: '/repo', branch: 'main', head: 'abc123', langs: ['python', 'typescript'],
  files: [
    ['src/app/big.py', 2000, 0, 100],
    ['src/app/small.py', 40, 0, 300],
    ['src/util.py', 120, 0, 200],
    ['web/app.ts', 300, 1, 400],
    ['web/lib/a.ts', 80, 1, 50],
  ],
  edges: [[1, 0], [0, 2], [3, 4], [9, 9]],
}

describe('codebase model', () => {
  it('knows a codebase id by its prefix, and round-trips its file', () => {
    expect(isCodebaseId(`${CODEBASE_ID_PREFIX}${'0'.repeat(27)}`)).toBe(true)
    expect(isCodebaseId('b0a4d' + '0'.repeat(27))).toBe(false)
    const doc = parseCodebaseDoc(serializeCodebaseDoc({ kind: 'kirocrew-codebase', version: 1, root: '/r', lens: 'graph', colorBy: 'recency', asks: [{ id: 'a1', nodeIds: ['file:x'] }] }))
    expect(doc).toMatchObject({ root: '/r', lens: 'graph', colorBy: 'recency', asks: [{ id: 'a1', nodeIds: ['file:x'] }] })
    expect(parseCodebaseDoc('{"lens":"bogus","asks":[{"id":1}]}')).toMatchObject({ lens: 'city', asks: [] })
    expect(parseCodebaseDoc('not json').root).toBe('')
  })

  it('decodes a scan: imports both ways, folder totals, and drops edges to missing rows', () => {
    const m = decodeScan(SCAN)
    expect(m.imports[1]).toEqual([0])
    expect(m.importedBy[0]).toEqual([1])
    expect(m.imports[0]).toEqual([2])
    expect(m.dirs.get('.')).toMatchObject({ files: 5, lines: 2540 })
    expect(m.dirs.get('src')).toMatchObject({ files: 3, lines: 2160 })
    expect(m.dirs.get('src/app')).toMatchObject({ files: 2, lines: 2040 })
  })

  it('squarify fills its rect exactly, with no overlaps', () => {
    const rects = squarify([50, 30, 10, 6, 4], { x: 0, z: 0, w: 10, d: 10 })
    const area = rects.reduce((s, r) => s + r.w * r.d, 0)
    expect(area).toBeCloseTo(100, 6)
    rects.forEach((r, i) => {
      expect(r.x).toBeGreaterThanOrEqual(-1e-9); expect(r.x + r.w).toBeLessThanOrEqual(10 + 1e-9)
      expect(r.w * r.d).toBeCloseTo([50, 30, 10, 6, 4][i], 6)
      for (const o of rects.slice(i + 1)) {
        const overlap = Math.min(r.x + r.w, o.x + o.w) - Math.max(r.x, o.x) > 1e-6 && Math.min(r.z + r.d, o.z + o.d) - Math.max(r.z, o.z) > 1e-6
        expect(overlap).toBe(false)
      }
    })
  })

  it('the city: a long file is a tall, wide tower; buildings sit inside their district and never overlap', () => {
    const city = layoutCity(decodeScan(SCAN))
    const big = city.buildings.find(b => b.path === 'src/app/big.py')!
    const small = city.buildings.find(b => b.path === 'src/app/small.py')!
    expect(big.h).toBeGreaterThan(small.h * 10)
    expect(big.w * big.d).toBeGreaterThan(small.w * small.d * 10)
    const district = city.districts.find(d => d.path === 'src/app')!
    for (const b of [big, small]) {
      expect(b.x - b.w / 2).toBeGreaterThanOrEqual(district.x - district.w / 2)
      expect(b.x + b.w / 2).toBeLessThanOrEqual(district.x + district.w / 2)
      expect(b.base).toBeCloseTo(district.base + district.h)
    }
    for (const [i, a] of city.buildings.entries()) for (const b of city.buildings.slice(i + 1)) {
      const overlap = Math.abs(a.x - b.x) < (a.w + b.w) / 2 - 1e-6 && Math.abs(a.z - b.z) < (a.d + b.d) / 2 - 1e-6
      expect(overlap).toBe(false)
    }
    expect(city.anchors.get('file:src/app/big.py')?.y).toBeCloseTo(big.base + big.h)
    expect(city.anchors.has('dir:web/lib')).toBe(true)
  })

  it('the graph joins folders whose files import each other, and settles', () => {
    const g = graphOf(decodeScan(SCAN))
    expect(g.nodes.map(n => n.path)).toEqual(['src', 'src/app', 'web', 'web/lib'])
    const names = g.links.map(l => [g.nodes[l.a].path, g.nodes[l.b].path].sort().join(' - '))
    expect(names.sort()).toEqual(['src - src/app', 'web - web/lib'])
    let moved = Infinity
    for (let i = 0, a = 1; i < 300; i++, a *= 0.985) moved = stepGraph(g, a)
    expect(moved).toBeLessThan(0.05)
    expect(g.nodes.every(n => Number.isFinite(n.x + n.y + n.z))).toBe(true)
  })

  it('a hub folder that hundreds of folders import settles instead of flying apart', () => {
    // The real repo's shape: one folder everything imports. Summed spring pulls
    // used to overshoot each step until positions overflowed a 32-bit float.
    const files: [string, number, number, number][] = [['core/util.py', 500, 0, 1]]
    const edges: [number, number][] = []
    for (let i = 0; i < 300; i++) {
      files.push([`pkg${i}/mod.py`, 100, 0, 1])
      for (let k = 0; k < 5; k++) edges.push([i + 1, 0])
    }
    const g = graphOf(decodeScan({ sig: 'h', langs: ['python'], files, edges }))
    for (let i = 0; i < 60; i++) stepGraph(g, 1)
    for (let a = 1; a > 0.015; a *= 0.985) stepGraph(g, a)
    const reach = Math.max(...g.nodes.map(n => Math.max(Math.abs(n.x), Math.abs(n.y), Math.abs(n.z))))
    expect(reach).toBeLessThan(5000)
    expect(g.nodes.every(n => Number.isFinite(n.x + n.y + n.z))).toBe(true)
  })

  it('colours by recency by rank: the newest file is the hottest', () => {
    const m = decodeScan(SCAN)
    const c = recencyColors(m.files)
    expect(c[3]).toBe('#f0553c')
    expect(c[4]).toBe('#6b7a99')
  })

  it('tells the conductor about the codebase and what was picked', () => {
    const m = decodeScan(SCAN)
    expect(summarizeCodebase(m)).toContain('- src/app/big.py (2000, 1 / 1)')
    const d = describeNodes(m, ['file:src/app/big.py', 'dir:src/app', 'file:gone.py'])
    expect(d).toContain('2000 lines of python. Imports: src/util.py. Imported by: src/app/small.py.')
    expect(d).toContain('dir:src/app: folder, 2 files, 2040 lines. Largest: src/app/big.py (2000)')
    expect(d).toContain('file:gone.py: no longer in the tree')
  })

  it('reads a new root and a focus from a reply, and neither shows in the answer', () => {
    const reply = 'Made a worktree.\n```codebase-root\n/repo-cb-1\n```\n```codebase-focus\n["file:a.py", "nope", 3]\n```'
    expect(rootIn(reply)).toBe('/repo-cb-1')
    expect(rootIn('```codebase-root\nrelative/path\n```')).toBeNull()
    expect(focusIn(reply)).toEqual(['file:a.py'])
    expect(splitReply(reply).prose).toBe('Made a worktree.')
    expect(splitReply('Working on it\n```codebase-root\n/half').prose).toBe('Working on it')
  })

  it('the turn opens like a doc ask, names a worktree for edits, and never the person\'s checkout', () => {
    const m = decodeScan(SCAN)
    const turn = buildCodebaseTurn({ id: 'a1', request: 'why is this so tall?', model: m, selected: ['file:src/app/big.py'], followUp: false, docId: 'c0deb00000000000000000012345678' })
    expect(turn.startsWith('[Doc ask a1]\nwhy is this so tall?')).toBe(true)
    expect(turn).toContain('git -C /repo worktree add /repo-cb-12345678 -b codebase/12345678')
    expect(turn).toContain('Never change the person\'s own checkout')
    expect(turn).toContain('slug "doc-overlay-c0deb00000000000000000012345678"')
    expect(turn).toContain('file:src/app/big.py: 2000 lines')
  })

  it('a rescan marks new files and changed line counts, never on a different root', () => {
    const a = decodeScan(SCAN)
    const b = decodeScan({ ...SCAN, sig: 's2', files: [['src/app/big.py', 900, 0, 1], ['src/app/small.py', 40, 0, 1], ['src/app/split.py', 1100, 0, 1]] })
    expect([...changedPaths(a, b)].sort()).toEqual(['src/app/big.py', 'src/app/split.py'])
    expect(changedPaths(a, { ...b, root: '/other' }).size).toBe(0)
  })
})

// ---- the pane -----------------------------------------------------------------------------

const getDoc = api.getDoc as unknown as ReturnType<typeof vi.fn>
const saveDoc = api.saveDoc as unknown as ReturnType<typeof vi.fn>
const scan = api.codebaseScan as unknown as ReturnType<typeof vi.fn>
const send = sendTurn as unknown as ReturnType<typeof vi.fn>
const DOC = 'c0deb0123456789abcdef0123456789a'
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const renderPane = (project?: string) => render(
  <QueryClientProvider client={queryClient}>
    <Provider store={store}><CodebasePane docId={DOC} slotKey="cb-slot" title="Repo" project={project} /></Provider>
  </QueryClientProvider>,
)

describe('CodebasePane', () => {
  beforeEach(() => {
    getDoc.mockReset(); saveDoc.mockReset(); scan.mockReset(); send.mockReset()
    localStorage.clear()
    saveDoc.mockImplementation((_id: string, content: string) => Promise.resolve({ content, version: 'v2' }))
    scan.mockResolvedValue(SCAN)
    send.mockResolvedValue({ status: 'sent' })
  })

  it('a new session starts on its project and draws its scan', async () => {
    getDoc.mockResolvedValue({ content: '', version: '' })
    renderPane('/repo')
    const stub = await screen.findByTestId('scene-stub')
    expect(stub.dataset.files).toBe('5')
    expect(scan).toHaveBeenCalledWith('/repo', undefined)
    expect(JSON.parse(saveDoc.mock.calls[0][1]).root).toBe('/repo')
    expect(screen.getByTestId('codebase-branch').textContent).toContain('main · 5 files · 2,540 lines')
  })

  it('a project that is not a git checkout asks for one, says why, and opens the corrected path', async () => {
    getDoc.mockResolvedValue({ content: '', version: '' })
    const notGit = JSON.stringify({ error: 'fatal: not a git repository (or any of the parent directories): .git', code: 'not_a_git_tree' })
    scan.mockImplementation((root: string) => root === '/workspace'
      ? Promise.reject(new ApiError(400, 'not a git tree', notGit))
      : Promise.resolve(SCAN))
    renderPane('/workspace')
    expect((await screen.findByTestId('codebase-root-error')).textContent).toContain('/workspace')
    expect(screen.getByTestId('codebase-root-error').textContent).toContain('not a git repository')
    const input = screen.getByLabelText('Codebase path') as HTMLInputElement
    expect(input.value).toBe('/workspace')
    fireEvent.change(input, { target: { value: '/repo' } })
    fireEvent.submit(screen.getByTestId('codebase-root-form'))
    await screen.findByTestId('scene-stub')
    expect(screen.queryByTestId('codebase-root-form')).toBeNull()
    expect(JSON.parse(saveDoc.mock.calls.at(-1)![1]).root).toBe('/repo')
  })

  it('the repo chip opens the path box to switch codebases, and Cancel keeps the current one', async () => {
    getDoc.mockResolvedValue({ content: serializeCodebaseDoc({ ...parseCodebaseDoc(''), root: '/repo' }), version: 'v1' })
    renderPane()
    await screen.findByTestId('scene-stub')
    fireEvent.click(screen.getByTestId('codebase-branch'))
    expect((screen.getByLabelText('Codebase path') as HTMLInputElement).value).toBe('/repo')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByTestId('codebase-root-form')).toBeNull()
  })

  it('Browse opens the folder picker at the current path, and a picked folder opens', async () => {
    getDoc.mockResolvedValue({ content: '', version: '' })
    renderPane()
    await screen.findByTestId('codebase-root-form')
    expect(screen.queryByTestId('picker-stub')).toBeNull()
    fireEvent.click(screen.getByTestId('codebase-root-browse'))
    fireEvent.click(screen.getByText('pick /picked/repo'))
    await screen.findByTestId('scene-stub')
    expect(scan).toHaveBeenCalledWith('/picked/repo', undefined)
    expect(screen.queryByTestId('picker-stub')).toBeNull()
  })

  it('with no project it asks which codebase', async () => {
    getDoc.mockResolvedValue({ content: '', version: '' })
    renderPane()
    const form = await screen.findByTestId('codebase-root-form')
    fireEvent.change(screen.getByLabelText('Codebase path'), { target: { value: '/elsewhere' } })
    fireEvent.submit(form)
    await screen.findByTestId('scene-stub')
    expect(scan).toHaveBeenCalledWith('/elsewhere', undefined)
  })

  it('pick, press C, ask: the turn carries the picked files, and the pick clears', async () => {
    getDoc.mockResolvedValue({ content: serializeCodebaseDoc({ ...parseCodebaseDoc(''), root: '/repo' }), version: 'v1' })
    renderPane()
    fireEvent.click(await screen.findByText('src/app/big.py'))
    fireEvent.click(screen.getByText('src/util.py'), { shiftKey: true })
    expect(screen.getByTestId('codebase-pick-bar').textContent).toContain('big.py, util.py')
    fireEvent.keyDown(window, { key: 'c' })
    const input = await screen.findByTestId('codebase-ask-input')
    fireEvent.change(input, { target: { value: 'split this up' } })
    fireEvent.submit(input.closest('form')!)
    await waitFor(() => expect(send).toHaveBeenCalledTimes(1))
    const message: string = send.mock.calls[0][0].message
    expect(message).toMatch(/^\[Doc ask a\w+\]\nsplit this up/)
    expect(message).toContain('file:src/app/big.py: 2000 lines')
    expect(message).toContain('file:src/util.py: 120 lines')
    const saved = JSON.parse(saveDoc.mock.calls.at(-1)![1])
    expect(saved.asks[0].nodeIds).toEqual(['file:src/app/big.py', 'file:src/util.py'])
    expect(screen.queryByTestId('codebase-pick-bar')).toBeNull()
  })

  it('C does nothing with nothing picked, and the lens switch is saved', async () => {
    getDoc.mockResolvedValue({ content: serializeCodebaseDoc({ ...parseCodebaseDoc(''), root: '/repo' }), version: 'v1' })
    renderPane()
    await screen.findByTestId('scene-stub')
    fireEvent.keyDown(window, { key: 'c' })
    expect(screen.queryByTestId('codebase-ask-input')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Graph/ }))
    expect(screen.getByTestId('scene-stub').dataset.lens).toBe('graph')
    expect(JSON.parse(saveDoc.mock.calls.at(-1)![1]).lens).toBe('graph')
  })

  it('a finished reply naming a worktree moves the model there, once', async () => {
    getDoc.mockResolvedValue({ content: serializeCodebaseDoc({ ...parseCodebaseDoc(''), root: '/repo' }), version: 'v1' })
    const reply: ChatMessage = { role: 'assistant', content: 'On it.\n```codebase-root\n/repo-cb-1\n```', meta: { mid: 'm1' } } as ChatMessage
    expect(codebaseChangesFromMessages([reply])).toEqual([{ key: 'm1', root: '/repo-cb-1', focus: null }])
    act(() => { store.dispatch(setActiveSlot('cb-slot')); store.dispatch(replaceMessages([reply])) })
    renderPane()
    await waitFor(() => expect(scan).toHaveBeenCalledWith('/repo-cb-1', undefined))
    const roots = saveDoc.mock.calls.map(c => JSON.parse(c[1]).root)
    expect(roots.filter((r: string) => r === '/repo-cb-1')).toHaveLength(1)
    act(() => { store.dispatch(replaceMessages([])) })
  })
})
