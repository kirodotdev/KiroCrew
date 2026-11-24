import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import { seededTraits } from '../../components/CrewAvatar'
import en from '../../i18n/locales/en.json'
import { poseBodyDataUri, poseDataUri, poseEyesDataUri } from '../../lib/avatarPoses'
import { ghostDataUri } from '../../lib/kiroGhostAvatar'
import { faceArtFor } from './faces'
import { XR_WRITES_ENABLED, tryWrite } from './flags'
import type { XrNeed } from './model'
import { targetOf } from './writes'
import { commandFits, crewText, plainText, wrapLines } from './text'

const dir = __dirname
const sources = readdirSync(dir)
  .filter(f => /\.tsx?$/.test(f) && !f.includes('.test.'))
  .map(f => ({ file: f, text: readFileSync(join(dir, f), 'utf8') }))
/** Every source of the XR page, in every folder under it (`workspace/` too), by its path from here. */
const everywhere = (readdirSync(dir, { recursive: true }) as string[])
  .filter(f => /\.tsx?$/.test(f) && !f.includes('.test.'))
  .map(f => ({ file: f.split('\\').join('/'), text: readFileSync(join(dir, f), 'utf8') }))
const code = (text: string) => text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

const WRITE_CALLS = /api\.(approve|reject|decide\w*|answer\w*|send\w*|chatSend|memberThread|spawn\w*|createMember|deleteMember|memberDashboardRefetch|dismiss\w*)\b|\.(setGoal|removeGoal|setKeyResults)\(|method:\s*['"](POST|PUT|PATCH|DELETE)['"]/

/**
 * A reach of `writes.ts`, whatever spelling its path takes (`../writes`, `./../writes`, `@/pages/xr/writes`,
 * `../writes.js`): the path in any quotes, after `from` or in `import(...)`, whose last segment is `writes`.
 */
const writesReach = () => {
  const path = String.raw`(?:[^'"\`\s]*\/)?writes(?:\.[jt]sx?)?`
  return {
    reaches: new RegExp(String.raw`(?:\bfrom\s*|\bimport\s*\(\s*)(['"\`])${path}\1`, 'g'),
    named: new RegExp(String.raw`\bimport\s*\{([^}]*)\}\s*from\s*(['"\`])${path}\2`, 'g'),
  }
}

describe('writes are off by default and fenced when on', () => {
  const need = { kind: 'approval', requestId: 'r1', requestMid: 'm1', slot: 's1', origin: 'native' } as XrNeed

  it('has the write switch off in a default build', () => {
    expect(XR_WRITES_ENABLED).toBe(false)
  })

  it('with the switch off, refuses and calls nothing', async () => {
    const perform = vi.fn()
    expect(await tryWrite({ kind: 'approve', need }, false, perform)).toEqual({ ok: false, reason: 'disabled', detail: '' })
    expect(await tryWrite({ kind: 'send', bot: 'atlas', slot: 's1', text: 'hi' }, false, perform)).toMatchObject({ ok: false, reason: 'disabled' })
    expect(perform).not.toHaveBeenCalled()
  })

  it('with the switch on, performs exactly the write it was given', async () => {
    const perform = vi.fn().mockResolvedValue(undefined)
    expect(await tryWrite({ kind: 'reject', need }, true, perform)).toEqual({ ok: true })
    expect(perform).toHaveBeenCalledWith({ kind: 'reject', need })
  })

  it('reports a write that failed, so the scene never says "done" for it', async () => {
    const perform = vi.fn().mockRejectedValue(new Error('403'))
    expect(await tryWrite({ kind: 'approve', need }, true, perform)).toEqual({ ok: false, reason: 'failed', detail: '403' })
  })

  it('names exactly one request when it approves or rejects', () => {
    expect(targetOf(need)).toEqual({ origin: 'native', id: 'r1', slot: 's1', mid: 'm1' })
    expect(targetOf({ ...need, origin: 'coordinator' })).toEqual({ origin: 'coordinator', id: 'r1', slot: 's1', instance: 'm1' })
  })

  it('keeps every write call of the API client in one module', () => {
    for (const s of everywhere) expect(WRITE_CALLS.test(code(s.text)), s.file).toBe(s.file === 'writes.ts')
  })

  it('lets only the flag module call that module, and only the scene call the flag', () => {
    const importers = (name: string) => sources.filter(s => new RegExp(`from '\\./${name}'`).test(s.text)).map(s => s.file).sort()
    expect(sources.filter(s => /\bperformWrite\b/.test(code(s.text))).map(s => s.file).sort()).toEqual(['flags.ts', 'writes.ts'])
    expect(sources.filter(s => /\btryWrite\(/.test(code(s.text))).map(s => s.file).sort()).toEqual(['flags.ts', 'scene.ts'])
    expect(importers('writes')).toEqual(['flags.ts'])
  })

  it('counts every spelling of the module\'s path as a reach of `writes.ts`: it is matched by its last segment', () => {
    const { reaches, named } = writesReach()
    for (const path of ['../writes', '../writes.ts', '../writes.js', './../writes', '@/pages/xr/writes', '../../xr/writes']) {
      expect(`import { cardSend } from '${path}'`.match(reaches), path).not.toBeNull()
      expect([...`import { cardSend } from "${path}"`.matchAll(named)].map(m => m[1].trim()), path).toEqual(['cardSend'])
      expect(`const w = await import(\`${path}\`)`.match(reaches), path).not.toBeNull()
    }
    // Another module whose name only ends or begins the same way is no reach.
    for (const path of ['../rewrites', '../writesLog', './writes/other']) expect(`import { x } from '${path}'`.match(reaches), path).toBeNull()
  })

  it('lets the room of pages reach one named write for each thing of its own, and nothing else', () => {
    const ws = readdirSync(join(dir, 'workspace')).filter(f => /\.tsx?$/.test(f) && !f.includes('.test.')).map(f => ({ file: f, text: readFileSync(join(dir, 'workspace', f), 'utf8') }))
    // The whole fence of the room: each file that reaches `writes.ts`, and the names it imports from there.
    // A line whose file is not in this tree holds nothing here; with every file there it reads
    //   from '../writes'  ->  ['SendNumbersTile.tsx', 'useBoards.ts', 'useChatCard.ts', 'useComment.ts', 'useDash3d.ts']
    // and each name is in the files that list it alone: `cardSend` is the send of words from XR, the card's, a
    // note's on a board (TALK slice 2) and the readout's numbers to the Bot that `sendto` names (P7 3); `cutRetry` is
    // a failed sheet's "Try again" (P3 slice 2).
    const fence: Record<string, string[]> = { 'SendNumbersTile.tsx': ['cardSend'], 'useBoards.ts': ['boardAnswer', 'boardDecide', 'cutRetry'], 'useChatCard.ts': ['cardChatLeft', 'cardSend'], 'useComment.ts': ['cardSend'], 'useDash3d.ts': ['dashRead'] }
    const owners = (name: string) => Object.keys(fence).filter(f => fence[f].includes(name) && here(f))
    const here = (file: string) => ws.some(s => s.file === file)
    const { reaches, named } = writesReach()
    expect(ws.filter(s => code(s.text).match(reaches)).map(s => s.file)).toEqual(Object.keys(fence).filter(here))
    for (const [file, names] of Object.entries(fence)) {
      const text = code(ws.find(s => s.file === file)?.text ?? '')
      // Every reach is one plain import of names, and the names are exactly the file's own: no `* as`, no `import()`.
      const lists = [...text.matchAll(named)].map(m => m[1].split(',').map(n => n.trim()).filter(Boolean))
      expect(lists.length, file).toBe((text.match(reaches) ?? []).length)
      expect(lists.flat().sort(), file).toEqual(here(file) ? names : [])
      for (const name of names) expect(ws.filter(s => new RegExp(`\\b${name}\\b`).test(code(s.text))).map(s => s.file), name).toEqual(owners(name))
    }
    for (const s of ws) expect(WRITE_CALLS.test(code(s.text)) || /\b(performWrite|tryWrite)\b/.test(code(s.text)), s.file).toBe(false)
  })

  it('reaches a write in the scene only from the router\'s write hook', () => {
    const scene = code(sources.find(s => s.file === 'scene.ts')!.text)
    // `tryWrite(` appears only inside the method the router calls for a committed guarded press.
    const body = scene.slice(scene.indexOf('private write(id: string, version: string)'), scene.indexOf('private failed('))
    expect((scene.match(/\btryWrite\(/g) ?? []).length).toBe((body.match(/\btryWrite\(/g) ?? []).length)
    expect((scene.match(/this\.write\(/g) ?? []).length).toBe(1)
    expect(scene).toContain('write: (id, version) => this.write(id, version)')
  })

  it('makes no raw network call of its own', () => {
    for (const s of everywhere) expect(/\bfetch\(|XMLHttpRequest|WebSocket\(|sendBeacon|EventSource\(/.test(code(s.text)), s.file).toBe(false)
  })

  it('reads every folder of the page, not only its top one', () => {
    expect(everywhere.some(s => s.file === 'workspace/useBoards.ts') && everywhere.some(s => s.file === 'writes.ts')).toBe(true)
  })

  it('uses only the API calls it names', () => {
    const calls = new Set(everywhere.flatMap(s => [...code(s.text).matchAll(/\bapi\.([A-Za-z]+)/g)].map(m => m[1])))
    // In the room of pages (`workspace/`): a page he opened in a browser panel is driven by his own pointer and keys,
    // and asked what it shows (`browserPanelInput`, `browserPanelAsk`, `streamSource.ts`); the config is read for
    // the pages' own settings (`kirocrewConfig`). A failed sheet's "Try again" picks a cut turn up (`continueSlot`).
    expect([...calls].sort()).toEqual(['answerQuestion', 'approvals', 'browserPanelAsk', 'browserPanelInput', 'chatSlotDetail', 'continueSlot', 'createChatSlot', 'decideApproval', 'kirocrewConfig', 'memberBriefing', 'memberDashboard', 'memberDashboardRefetch', 'memberGoals', 'memberThread', 'pendingQuestions', 'sendChat'])
  })
})

describe('crew content is data, never code', () => {
  it('never turns text into markup or script', () => {
    for (const s of everywhere) {
      expect(/innerHTML|outerHTML\s*=|insertAdjacentHTML|dangerouslySetInnerHTML|\beval\(|new Function|document\.write/.test(code(s.text)), s.file).toBe(false)
      // A frame is the dashboard's own page drawn in the room (`workspace/domSource.ts`), never crew text.
      expect(/srcdoc|<iframe/.test(code(s.text)), s.file).toBe(s.file === 'workspace/domSource.ts')
    }
  })

  it('keeps a Bot message as the characters it is: markup is text to draw, never markup', () => {
    expect(plainText('Hi <img src=x onerror=alert(1)> **there** [link](javascript:void0)')).toBe('Hi <img src=x onerror=alert(1)> there link (javascript:void0)')
  })

  it('shows control and direction-override characters as escapes', () => {
    expect(crewText('a\u202Eb\u0007c')).toBe('a\\u202eb\\u0007c')
  })

  it('drops the glyphs the scene uses for its own state', () => {
    expect(crewText('\u2713 approved \u25CF')).toBe(' approved ')
  })

  it('offers Approve only when the whole command can be read', () => {
    const measure = (s: string) => s.length * 10
    expect(commandFits(measure, 'npm test', 200, 4)).toBe(true)
    expect(commandFits(measure, 'x'.repeat(200), 200, 4)).toBe(false)
  })

  it('wraps long words and marks what it cut', () => {
    const measure = (s: string) => s.length
    expect(wrapLines(measure, 'aaaa bbbb cccc', 9).lines).toEqual(['aaaa bbbb', 'cccc'])
    expect(wrapLines(measure, 'abcdefghij', 4).lines).toEqual(['abcd', 'efgh', 'ij'])
    expect(wrapLines(measure, 'one two three four', 5, 2)).toEqual({ lines: ['one', 'two…'], clipped: true })
  })
})

describe('XR speaks of Bots and their work', () => {
  it('has no wording about sessions', () => {
    for (const [key, value] of Object.entries((en as unknown as { xr: Record<string, string> }).xr)) {
      expect(/session|slot\b/i.test(value), key).toBe(false)
    }
  })
})

describe('a face in XR is the face the dashboard shows', () => {
  it('uses the pose icon and its colour, cut into tile, body and eyes', () => {
    const art = faceArtFor({ kind: 'icon', pose: 'pose-3', bg: '#9046ff' }, 'Atlas')
    expect(art.kind).toBe('icon')
    expect(art.layers.map(l => l.id)).toEqual(['tile', 'body', 'eyes'])
    expect(art.layers[1].uri).toBe(poseBodyDataUri('pose-3'))
    expect(art.layers[2].uri).toBe(poseEyesDataUri('pose-3'))
    expect(art.flat).toBe(poseDataUri('pose-3', '#9046ff'))
    expect(decodeURIComponent(art.layers[0].uri)).toContain('#9046ff')
  })

  it('uses the ghost seeded from the name when no face is pinned', () => {
    const art = faceArtFor({}, 'Juno')
    expect(art.kind).toBe('ghost')
    expect(art.flat).toBe(ghostDataUri(seededTraits('Juno')))
    expect(art.layers.map(l => l.id).slice(0, 3)).toEqual(['tile', 'body', 'eyes'])
    expect(decodeURIComponent(art.layers[0].uri)).toContain(seededTraits('Juno').tile)
  })

  it('stacks the layers front to back', () => {
    const depths = faceArtFor({}, 'Otto').layers.map(l => l.depth)
    expect([...depths].sort((a, b) => a - b)).toEqual(depths)
  })

  it('uses the uploaded picture as it is', () => {
    const art = faceArtFor({ kind: 'image', v: 7 }, 'Mira')
    expect(art.kind).toBe('image')
    expect(art.layers).toHaveLength(1)
    expect(art.flat).toMatch(/\/avatar\?v=7$/)
  })
})
