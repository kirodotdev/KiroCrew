import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import { buildSpritePackData, firstFramePreview } from './petdexImport'

// happy-dom never fires onload for a decoded sheet, so the browser image and canvas
// seams are faked here. The code under test (slicing, row maps, promise settling) is real.
const env = {
  height: 0,
  fail: false,
  exportError: null as Error | null,
  noContext: false,
  canvases: [] as FakeCanvas[],
  images: [] as FakeImage[],
}

class FakeImage {
  naturalHeight = env.height
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  private value = ''
  constructor() {
    env.images.push(this)
  }
  get src() {
    return this.value
  }
  set src(value: string) {
    this.value = value
    queueMicrotask(() => (env.fail ? this.onerror?.() : this.onload?.()))
  }
}

class FakeCanvas {
  width = 0
  height = 0
  ctx = { clearRect: vi.fn(), drawImage: vi.fn() }
  getContext() {
    return env.noContext ? null : this.ctx
  }
  toDataURL() {
    if (env.exportError) throw env.exportError
    return `data:image/png;base64,${this.width}x${this.height}`
  }
}

const META = { displayName: 'Pip', author: 'tester', description: 'a pet' }
const SHEET = 'c2hlZXQ='

beforeEach(() => {
  env.height = 9 * 208
  env.fail = false
  env.exportError = null
  env.noContext = false
  env.canvases = []
  env.images = []
  vi.stubGlobal('Image', FakeImage)
  const realCreate = document.createElement.bind(document)
  vi.spyOn(document, 'createElement').mockImplementation(((tag: string, options?: ElementCreationOptions) => {
    if (tag !== 'canvas') return realCreate(tag, options)
    const canvas = new FakeCanvas()
    env.canvases.push(canvas)
    return canvas
  }) as typeof document.createElement)
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('firstFramePreview', () => {
  it('crops the first idle frame and returns it as a PNG data URI', async () => {
    const out = await firstFramePreview(SHEET)
    expect(out).toBe('data:image/png;base64,192x208')
    expect(env.images[0].src).toBe(`data:image/webp;base64,${SHEET}`)
    const [canvas] = env.canvases
    expect(canvas.ctx.drawImage).toHaveBeenCalledWith(expect.any(FakeImage), 0, 0, 192, 208, 0, 0, 192, 208)
  })

  it('rejects when the sheet cannot be decoded', async () => {
    env.fail = true
    await expect(firstFramePreview(SHEET)).rejects.toThrow('Could not decode the sprite sheet')
  })

  it('rejects when the canvas cannot be read back', async () => {
    env.exportError = new Error('tainted canvas')
    await expect(firstFramePreview(SHEET)).rejects.toThrow('tainted canvas')
  })

  it('rejects when the canvas has no 2D context', async () => {
    env.noContext = true
    await expect(firstFramePreview(SHEET)).rejects.toThrow(TypeError)
  })
})

describe('buildSpritePackData', () => {
  it('slices every PetDex row into a strip and keys the pack by state and random clip', async () => {
    const pack = await buildSpritePackData(SHEET, META)
    expect(pack).toMatchObject({
      name: 'Pip',
      author: 'tester',
      description: 'a pet',
      frameWidth: 192,
      frameHeight: 208,
      fps: 7,
      flipX: false,
      offsetY: 0,
      rowAssignments: { idle: 0, error: 5, done: 4, walking: 1 },
    })
    expect(Object.keys(pack.assignments).sort()).toEqual(['done', 'error', 'idle', 'walking'])
    expect(Object.keys(pack.randomAssignments).sort()).toEqual(['review', 'run', 'waiting', 'wave'])
    expect(pack.assignments.idle).toBe('data:image/png;base64,1152x208')
  })

  it('reads each strip from its own grid row, using its frame count for the width', async () => {
    await buildSpritePackData(SHEET, META)
    const drawn = env.canvases.map((c) => c.ctx.drawImage.mock.calls[0])
    expect(drawn).toContainEqual([expect.any(FakeImage), 0, 0, 1152, 208, 0, 0, 1152, 208])
    expect(drawn).toContainEqual([expect.any(FakeImage), 0, 8 * 208, 1536, 208, 0, 0, 1536, 208])
  })

  it('skips rows that the sheet is too short to hold', async () => {
    env.height = 5 * 208
    const pack = await buildSpritePackData(SHEET, META)
    expect(pack.rowAssignments).toEqual({ idle: 0, done: 4, walking: 1 })
    expect(Object.keys(pack.randomAssignments)).toEqual(['wave'])
  })

  it('rejects a sheet that has no idle row', async () => {
    env.height = 100
    await expect(buildSpritePackData(SHEET, META)).rejects.toThrow('Sheet has no idle row')
  })

  it('rejects when the sheet cannot be decoded', async () => {
    env.fail = true
    await expect(buildSpritePackData(SHEET, META)).rejects.toThrow('Could not decode the PetDex sprite sheet')
  })

  it('rejects when slicing fails on a missing 2D context', async () => {
    env.noContext = true
    await expect(buildSpritePackData(SHEET, META)).rejects.toThrow(TypeError)
  })
})
