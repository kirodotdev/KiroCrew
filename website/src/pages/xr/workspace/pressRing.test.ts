import { readFileSync } from 'node:fs'
import ts from 'typescript'
import { describe, expect, it, vi } from 'vitest'
import { PRESS_RING, placePressRing } from './pressRing'

const css = () => ({ cssWidth: 1024, cssHeight: 720 })
/** A page of 0.8 m by 0.5 m whose middle stands 0.1 m right of and 0.05 m over the panel's own middle. */
const page = { cx: 0.1, cy: 0.05, w: 0.8, h: 0.5 }
const mesh = () => ({ visible: false, position: { set: vi.fn() } })

/**
 * What the panel's source holds, parsed and not read as text: a call or a mesh turned into a comment is no node,
 * and is not found. The calls of `placePressRing` inside a `useFrame`, and the meshes with a given `ref`.
 */
const panelNodes = () => {
  const file = ts.createSourceFile('Panel.tsx', readFileSync('src/pages/xr/workspace/Panel.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const inFrameLoop: string[] = []
  const jsx: ts.JsxElement[] = []
  const walk = (node: ts.Node, inLoop: boolean) => {
    if (ts.isCallExpression(node) && node.expression.getText() === 'placePressRing' && inLoop) inFrameLoop.push(node.getText())
    if (ts.isJsxElement(node) && node.openingElement.tagName.getText() === 'mesh') jsx.push(node)
    const loop = inLoop || (ts.isCallExpression(node) && node.expression.getText() === 'useFrame')
    ts.forEachChild(node, child => walk(child, loop))
  }
  walk(file, false)
  const meshes = (ref: string) => jsx.filter(m => m.openingElement.attributes.properties.some(a => a.getText() === `ref={${ref}}`)).map(m => m.getText())
  return { inFrameLoop, meshes }
}

describe('the ring at a press of a live page', () => {
  it('stands where the press was: the page\'s middle, a corner, any place', () => {
    const ring = mesh()
    placePressRing(ring, { x: 512, y: 360 }, css, page)
    expect(ring.visible).toBe(true)
    expect(ring.position.set).toHaveBeenLastCalledWith(0.1, 0.05, PRESS_RING.liftM)
    // The page's top left is the panel's left and its top (up is plus).
    placePressRing(ring, { x: 0, y: 0 }, css, page)
    expect(ring.position.set).toHaveBeenLastCalledWith(0.1 - 0.4, 0.05 + 0.25, PRESS_RING.liftM)
    placePressRing(ring, { x: 768, y: 540 }, css, page)
    const [x, y] = ring.position.set.mock.calls.at(-1) as number[]
    expect(x).toBeCloseTo(0.1 + 0.2, 9)
    expect(y).toBeCloseTo(0.05 - 0.125, 9)
  })

  it('is hidden when no mark stands, and the page is not asked for its size then', () => {
    const ring = mesh()
    placePressRing(ring, { x: 10, y: 10 }, css, page)
    ring.position.set.mockClear()
    const asked = vi.fn(css)
    placePressRing(ring, null, asked, page)
    expect(ring.visible).toBe(false)
    expect(ring.position.set).not.toHaveBeenCalled()
    expect(asked).not.toHaveBeenCalled()
    // A panel whose ring is not mounted yet is left alone.
    expect(() => placePressRing(null, { x: 1, y: 1 }, css, page)).not.toThrow()
  })

  it('is a ring with an open middle, wider than the page\'s own pointer ring, in front of the page', () => {
    expect(PRESS_RING.inner).toBeGreaterThan(0)
    expect(PRESS_RING.outer).toBeGreaterThan(PRESS_RING.inner)
    expect(PRESS_RING.liftM).toBeGreaterThan(0)
    // The pointer's own ring is drawn out to 2.4 rims: the press ring stands round it.
    const pointerRing = panelNodes().meshes('mark')
    expect(pointerRing).toHaveLength(1)
    expect(pointerRing[0]).toContain('<ringGeometry args={[rim * 1.6, rim * 2.4, 24]} />')
    expect(PRESS_RING.inner).toBeGreaterThan(2.4)
  })

  it('is drawn by the panel: it mounts the ring, takes no pointer with it, and places it each frame from the source\'s mark', () => {
    const { inFrameLoop, meshes } = panelNodes()
    const ringMeshes = meshes('pressRing')
    // One call, in the panel's frame loop, from the source's own mark.
    expect(inFrameLoop).toEqual(['placePressRing(pressRing.current, source.pressMark?.() ?? null, () => source.frame(), at)'])
    // One mesh: hidden until a mark stands, it takes no pointer, and its shape is the ring's.
    expect(ringMeshes).toHaveLength(1)
    const open = ringMeshes[0].slice(0, ringMeshes[0].indexOf('>') + 1)
    expect(open).toBe('<mesh ref={pressRing} visible={false} renderOrder={5} pointerEvents="none">')
    expect(ringMeshes[0]).toContain('<ringGeometry args={[rim * PRESS_RING.inner, rim * PRESS_RING.outer, 32]} />')
  })
})
