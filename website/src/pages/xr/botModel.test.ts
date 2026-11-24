import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * The model file itself. The shader reads a flick by the number of its bone (3 and 4, `botShader.ts`), and
 * `Bot.tsx` states the file's size and counts: a file made again in another order or shape fails here, not in the picture.
 */
interface Accessor { count: number }
interface Primitive { attributes: Record<string, number>; indices: number }
interface Gltf {
  scene: number
  scenes: { name: string; nodes: number[] }[]
  nodes: { name?: string }[]
  skins: { joints: number[] }[]
  meshes: { name: string; primitives: Primitive[]; extras?: { targetNames?: string[] } }[]
  accessors: Accessor[]
  textures?: unknown[]
  images?: unknown[]
}

const raw = readFileSync(resolve(process.cwd(), 'public/vendor/xr-bot/ghost.glb'))
const jsonBytes = raw.readUInt32LE(12)
const gltf = JSON.parse(raw.subarray(20, 20 + jsonBytes).toString('utf8')) as Gltf
const triangles = (name: string) => {
  const mesh = gltf.meshes.find(m => m.name === name)
  if (!mesh) throw new Error(`the file has no mesh "${name}"`)
  return gltf.accessors[mesh.primitives[0].indices].count / 3
}

describe('the body file of a Bot', () => {
  it('keeps its bones in the order the shader counts them in', () => {
    expect(gltf.skins).toHaveLength(1)
    expect(gltf.skins[0].joints.map(j => gltf.nodes[j].name)).toEqual(['belly', 'head', 'hem', 'flick', 'flick2'])
  })

  it('has the size and the counts that Bot.tsx states', () => {
    expect(raw.length).toBe(320_844)
    expect(triangles('body')).toBe(6_136)
    expect(triangles('eyes')).toBe(168)
    expect(triangles('wear')).toBe(2_994)
    expect(gltf.meshes.find(m => m.name === 'eyes')?.extras?.targetNames).toEqual(['blink', 'wide', 'lookX', 'lookY'])
    expect(gltf.textures ?? []).toHaveLength(0)
    expect(gltf.images ?? []).toHaveLength(0)
  })

  it('shows its first scene, the Bot, and keeps the things to wear in the second', () => {
    expect(gltf.scene).toBe(0)
    expect(gltf.scenes.map(s => s.name)).toEqual(['ghost', 'wardrobe'])
  })
})
