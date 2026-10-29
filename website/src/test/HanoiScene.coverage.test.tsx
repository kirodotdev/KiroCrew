import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer from '../store/chatSlice'
import HanoiScene from '../pages/scenes/HanoiScene'
import { markAgentsKnown } from '../hooks/sceneStateCache'
import { SPEECH_BUBBLE_MS } from '../hooks/sceneText'
import { SCENE_SCALE } from '../pages/scenes/config'
import type { AgentSource } from '../hooks/useAgentSync'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      chatSlotDetail: vi.fn(() => Promise.resolve({ messages: [] })),
      createChatSlot: vi.fn(() => Promise.resolve({})),
    },
  }
})

interface FillRecord { x: number; y: number; w: number; h: number; color: string }
interface TextRecord { text: string; x: number; y: number }

interface Recorder {
  ctx: CanvasRenderingContext2D
  fills: FillRecord[]
  texts: TextRecord[]
  images: number
}

function createRecorder(): Recorder {
  const fills: FillRecord[] = []
  const texts: TextRecord[] = []
  const counts = { images: 0 }
  const gradient = { addColorStop: vi.fn() }
  const store: Record<string, unknown> = {
    fillStyle: '#000000',
    strokeStyle: '#000000',
    globalAlpha: 1,
    font: '',
    textAlign: 'start',
    textBaseline: 'alphabetic',
    imageSmoothingEnabled: false,
    canvas: { width: 0, height: 0 },
    createLinearGradient: vi.fn(() => gradient),
    createRadialGradient: vi.fn(() => gradient),
    createPattern: vi.fn(() => null),
    measureText: vi.fn((t: string) => ({ width: t.length * 8 })),
    getImageData: vi.fn(() => ({ data: new Uint8ClampedArray(4) })),
  }
  store.fillRect = vi.fn((x: number, y: number, w: number, h: number) => {
    fills.push({ x, y, w, h, color: String(store.fillStyle) })
  })
  store.fillText = vi.fn((text: string, x: number, y: number) => {
    texts.push({ text, x, y })
  })
  store.drawImage = vi.fn(() => { counts.images++ })
  const ctx = new Proxy(store, {
    get(target, prop) {
      const key = String(prop)
      if (!(key in target)) target[key] = vi.fn()
      return target[key]
    },
    set(target, prop, value) {
      target[String(prop)] = value
      return true
    },
  }) as unknown as CanvasRenderingContext2D
  return {
    ctx,
    fills,
    texts,
    get images() { return counts.images },
  }
}

let recorders: Recorder[] = []
let queuedFrames = new Map<number, FrameRequestCallback>()
let frameSeq = 0
let nowMs = 1_700_000_000_000

beforeEach(() => {
  recorders = []
  queuedFrames = new Map()
  frameSeq = 0
  nowMs = 1_700_000_000_000
  HTMLCanvasElement.prototype.getContext = vi.fn(() => {
    const rec = createRecorder()
    recorders.push(rec)
    return rec.ctx
  }) as unknown as HTMLCanvasElement['getContext']
  vi.spyOn(Math, 'random').mockImplementation(() => 0.25)
  vi.spyOn(Date, 'now').mockImplementation(() => nowMs)
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    const id = ++frameSeq
    queuedFrames.set(id, cb)
    return id
  })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => { queuedFrames.delete(id) })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  localStorage.clear()
})

function runFrames(n: number) {
  act(() => {
    for (let i = 0; i < n; i++) {
      const due = [...queuedFrames.values()]
      queuedFrames.clear()
      due.forEach(cb => cb(0))
    }
  })
}

function agent(over: Partial<AgentSource> & { id: string }): AgentSource {
  return { name: over.id, label: 'default', kind: 'slot', running: false, detail: '', ...over }
}

function seated(agents: AgentSource[]) {
  markAgentsKnown('hanoi', agents.map(a => a.id))
  return agents
}

function mount(agents: AgentSource[], visible = true) {
  const store = configureStore({ reducer: { chat: chatReducer } })
  const base = recorders.length
  const tree = (list: AgentSource[], show: boolean) => (
    <Provider store={store}>
      <MemoryRouter><HanoiScene agents={list} visible={show} /></MemoryRouter>
    </Provider>
  )
  const view = render(tree(agents, visible))
  const rerender = (next: AgentSource[], nextVisible = visible) => view.rerender(tree(next, nextVisible))
  return { ...view, rerender, pixel: recorders[base], overlay: recorders[base + 1] }
}

const labels = (rec: Recorder) => rec.texts.map(t => t.text)
const colors = (rec: Recorder) => new Set(rec.fills.map(f => f.color))

function lastFrameLabels(rec: Recorder, frames = 1) {
  rec.texts.length = 0
  rec.fills.length = 0
  runFrames(frames)
  return labels(rec)
}

describe('HanoiScene mounting', () => {
  it('renders the pixel canvas and the text overlay with accessible labels', () => {
    const view = mount([])
    expect(view.getByLabelText('Hà Nội old quarter animation')).toBeInTheDocument()
    expect(view.getByLabelText('Hà Nội old quarter text overlay')).toBeInTheDocument()
  })

  it('keeps the pixel canvas unsmoothed and paints the static layers once', () => {
    const view = mount([])
    const canvas = view.getByLabelText('Hà Nội old quarter animation') as HTMLCanvasElement
    expect(canvas.style.imageRendering).toBe('pixelated')
    expect(recorders.length).toBe(6)
    const layerFills = recorders.slice(2).map(r => r.fills.length)
    runFrames(3)
    expect(recorders.slice(2).map(r => r.fills.length)).toEqual(layerFills)
  })
})

describe('HanoiScene landmarks and signs', () => {
  it('writes the title, subtitle and street-stall signs but leaves the bank unlabelled', () => {
    const { overlay } = mount([])
    expect(labels(overlay)).toEqual(expect.arrayContaining([
      'Hà Nội Old Quarter',
      'Hoàn Kiếm lakeside',
      'CÀ PHÊ TRỨNG',
      'PHỞ BÒ',
      'TRÀ ĐÁ',
    ]))
    expect(labels(overlay)).not.toContain('PVcomBank')
  })

  it('paints the lit tower, the red bridge and the bank band into the static layers', () => {
    mount([])
    const layerColors = new Set(recorders.slice(2).flatMap(r => r.fills.map(f => f.color)))
    expect(layerColors).toContain('#eadba3')
    expect(layerColors).toContain('#ff4a3a')
    expect(layerColors).toContain('#c79f00')
  })

  it('composites the static layers onto the pixel canvas every frame', () => {
    const { pixel } = mount([])
    const before = pixel.images
    runFrames(1)
    expect(pixel.images).toBeGreaterThan(before + 5)
  })

  it('switches one office-tower window on or off every so often, like late overtime', () => {
    const { pixel } = mount([])
    const scale = SCENE_SCALE
    const litPanes = () => pixel.fills.filter(f =>
      (f.color === '#d9c48e' || f.color === '#c3cdc6')
      && f.x >= 12 * scale && f.x < 36 * scale
      && f.y >= 69 * scale && f.y < 87 * scale,
    ).length
    const before = litPanes()
    expect(before).toBeGreaterThan(0)
    runFrames(99)
    pixel.fills.length = 0
    runFrames(1)
    expect(Math.abs(litPanes() - before)).toBe(1)
  })

  it('reports an empty café in the counter', () => {
    const { overlay } = mount([])
    expect(labels(overlay)).toContain('0/8 at the café')
  })

  it('draws nothing while hidden and starts drawing once visible', () => {
    const { overlay, rerender } = mount([], false)
    runFrames(5)
    expect(overlay.texts).toHaveLength(0)
    rerender([], true)
    runFrames(1)
    expect(labels(overlay)).toContain('Hà Nội Old Quarter')
  })
})

describe('HanoiScene agents', () => {
  it('labels every seated agent with name and detail and counts them', () => {
    const agents = seated([
      agent({ id: 'slot-linh', name: 'Linh', running: true, detail: '4 msgs' }),
      agent({ id: 'slot-minh', name: 'Minh', detail: 'idle' }),
      agent({ id: 'cron-night', name: 'Nightly', kind: 'cron', detail: '*/15 * * * *' }),
    ])
    const { overlay } = mount(agents)
    expect(labels(overlay)).toEqual(expect.arrayContaining(['Linh', '4 msgs', 'Minh', 'idle', 'Nightly', '*/15 * * * *']))
    expect(labels(overlay)).toContain('3/8 at the café')
  })

  it('caps the café at eight seats', () => {
    const many = seated(Array.from({ length: 11 }, (_, i) => agent({ id: `slot-many-${i}` })))
    const { overlay } = mount(many)
    expect(labels(overlay)).toContain('8/8 at the café')
  })

  it('shows a glowing laptop for a running agent and a coffee cup for an idle one', () => {
    const running = mount(seated([agent({ id: 'slot-run', running: true })]))
    expect(colors(running.pixel)).toContain('#7ee8c8')
    expect(colors(running.pixel)).toContain('#7ef7b0')
    running.unmount()

    const idle = mount(seated([agent({ id: 'slot-idle', running: false })]))
    expect(colors(idle.pixel)).toContain('#f7d774')
    expect(colors(idle.pixel)).not.toContain('#7ee8c8')
  })

  it('walks a new agent in from the edge of the scene before labelling it', () => {
    const { overlay } = mount([agent({ id: 'slot-new', name: 'Newcomer' })])
    expect(labels(overlay)).not.toContain('Newcomer')
    expect(lastFrameLabels(overlay, 200)).toContain('Newcomer')
  })

  it('picks up a rename and running-state change in place', () => {
    const first = seated([agent({ id: 'slot-r', name: 'Before', running: false })])
    const { overlay, pixel, rerender } = mount(first)
    rerender([agent({ id: 'slot-r', name: 'After', running: true })])
    expect(lastFrameLabels(overlay)).toContain('After')
    expect(colors(pixel)).toContain('#7ee8c8')
  })
})

describe('HanoiScene speech bubbles', () => {
  it('pops a bubble when the latest message changes and drops it after it expires', () => {
    const base = seated([agent({ id: 'slot-b', name: 'Talker', lastMessage: 'old' })])
    const { overlay, rerender } = mount(base)
    rerender([agent({ id: 'slot-b', name: 'Talker', lastMessage: 'Xin chào' })])
    expect(lastFrameLabels(overlay)).toContain('Xin chào')
    nowMs += SPEECH_BUBBLE_MS + 10
    expect(lastFrameLabels(overlay)).not.toContain('Xin chào')
  })

  it('lets two seated neighbours chat while nobody has a fresh message', () => {
    const { overlay } = mount(seated([agent({ id: 'slot-c1' }), agent({ id: 'slot-c2' })]))
    expect(labels(overlay)).toEqual(expect.arrayContaining(['Deployed yet?', 'Building now!']))
  })
})
