import { describe, it, expect } from 'vitest'

/** BLOCKING F1 -- a tombstone must record the incarnation of the session
 *  it is closing, and must be armed before the DELETE leaves.
 *
 *  Two independent halves, so two independent pins. An optimistic row built without the
 *  incarnation the create/resume reply already carries makes `slotCloseStarted` stamp
 *  `undefined`, so the differing-incarnation reveal can never fire. And a site that armed the
 *  wrapper AFTER its DELETE had landed left a gap in which a replacement could be created and
 *  listed -- the tombstone then stamped the REPLACEMENT and hid a live row indefinitely.
 *
 *  Source pins rather than store drivers: the defect is which value each construction site
 *  passes and where the arming sits, and both are properties of the call sites themselves. */

const read = (glob: Record<string, { default: string }>): string =>
  Object.values(glob)[0].default

const chatSlice = read(import.meta.glob('../store/chatSlice.ts', { query: '?raw', eager: true }))
const artifactPage = read(import.meta.glob('../pages/ArtifactDetailPage.tsx', { query: '?raw', eager: true }))
const papyrus = read(import.meta.glob('../apps/papyrus/PapyrusPage.tsx', { query: '?raw', eager: true }))

/** Every `addSlotOptimistic({...})` object literal in one source. */
const optimisticLiterals = (src: string): string[] => {
  const out: string[] = []
  const needle = 'addSlotOptimistic({'
  for (let i = src.indexOf(needle); i !== -1; i = src.indexOf(needle, i + 1)) {
    out.push(src.slice(i, i + 700))
  }
  return out
}

describe('every optimistic row carries the incarnation', () => {
  it('finds the construction sites it means to check', () => {
    // Positive control: a pin over zero sites would pass vacuously.
    expect(optimisticLiterals(chatSlice).length).toBeGreaterThanOrEqual(2)
    expect(optimisticLiterals(artifactPage).length).toBe(2)
    expect(optimisticLiterals(papyrus).length).toBe(1)
  })

  /** Two admissible shapes, and only two: a literal that NAMES the incarnation, or a whole-row
   *  spread, which carries every field the row already had. A row built field-by-field without
   *  it stamps the close `undefined`, so the differing-incarnation reveal never fires. */
  it('names incarnation in each one, or spreads a row that already has it', () => {
    for (const [name, src] of [
      ['chatSlice', chatSlice],
      ['ArtifactDetailPage', artifactPage],
      ['PapyrusPage', papyrus],
    ] as const) {
      for (const literal of optimisticLiterals(src)) {
        expect(literal, `${name}: optimistic row omits incarnation`).toMatch(/incarnation|\.\.\./)
      }
    }
  })
})

describe('the close wrapper is armed around the DELETE', () => {
  /** The main close path runs the DELETE as the wrapper's operation, so the key is withheld
   *  before the request leaves. */
  it('wraps the main close path DELETE inside the wrapper', () => {
    const i = chatSlice.indexOf('withSlotClose(dispatch, () => getState() as RootState')
    expect(i).toBeGreaterThan(-1)
    expect(chatSlice.slice(i, i + 260)).toMatch(/async \(\) => \{[\s\S]{0,120}?closeSlotOnServer/)
  })

  /** The artifact page's "New chat" archives its bound slots the same way. An EMPTY operation
   *  is the shape of the defect: it means the DELETE already ran outside the hold. */
  it('runs the artifact page DELETE inside the wrapper rather than after it', () => {
    const i = artifactPage.indexOf('withSlotClose(')
    expect(i).toBeGreaterThan(-1)
    const call = artifactPage.slice(i, i + 420)
    expect(call, 'artifact page arms an empty operation').not.toMatch(/async \(\) => \{\}\)/)
    expect(call).toMatch(/deleteChatSlot/)
  })

  it('arms exactly once per bound slot', () => {
    const armings = artifactPage.split('withSlotClose(').length - 1
    expect(armings).toBe(1)
  })
})
