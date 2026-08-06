import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer from '../store/chatSlice'
import OfficeScene from '../pages/scenes/OfficeScene'
import NeuralConstellationScene from '../pages/scenes/NeuralConstellationScene'
import WizardTowerScene from '../pages/scenes/WizardTowerScene'
import UnderwaterLabScene from '../pages/scenes/UnderwaterLabScene'
import type { AgentSource } from '../hooks/useAgentSync'

// Canvas 2D context mock — jsdom doesn't implement it.
// Use a Proxy so any method call returns a no-op fn and any property access returns a sensible default.
function createMockCtx(): CanvasRenderingContext2D {
  const gradientStub = { addColorStop: vi.fn() }
  const overrides: Record<string, unknown> = {
    createLinearGradient: vi.fn(() => gradientStub),
    createRadialGradient: vi.fn(() => gradientStub),
    measureText: vi.fn(() => ({ width: 50 })),
    getImageData: vi.fn(() => ({ data: new Uint8ClampedArray(4) })),
    createPattern: vi.fn(() => null),
    imageSmoothingEnabled: false,
    canvas: { width: 800, height: 600 },
  }
  return new Proxy(overrides, {
    get(target, prop) {
      if (prop in target) return target[prop as string]
      // Return a no-op fn for any method, 0/'' for unknown props
      target[prop as string] = vi.fn()
      return target[prop as string]
    },
    set(target, prop, value) {
      target[prop as string] = value
      return true
    },
  }) as unknown as CanvasRenderingContext2D
}

const mockAgents: AgentSource[] = [
  { id: 'slot-1', name: 'Alpha', label: 'default', kind: 'slot', running: true, detail: '3 msgs' },
  { id: 'slot-2', name: 'Beta', label: 'gpt4', kind: 'slot', running: false, detail: '1 msgs' },
]

beforeEach(() => {
  HTMLCanvasElement.prototype.getContext = vi.fn(() => createMockCtx())
})

afterEach(() => {
  vi.restoreAllMocks()
})

function renderScene(ui: React.ReactElement) {
  const store = configureStore({ reducer: { chat: chatReducer } })
  return render(<Provider store={store}><MemoryRouter>{ui}</MemoryRouter></Provider>)
}

describe('OfficeScene', () => {
  it('renders a canvas element', () => {
    const { container } = renderScene(<OfficeScene agents={[]} />)
    expect(container.querySelector('canvas')).toBeInTheDocument()
  })

  it('renders canvas when visible=true', () => {
    const { container } = renderScene(<OfficeScene agents={mockAgents} visible={true} />)
    expect(container.querySelector('canvas')).toBeInTheDocument()
  })

  it('renders canvas when visible=false (kept mounted)', () => {
    const { container } = renderScene(<OfficeScene agents={[]} visible={false} />)
    expect(container.querySelector('canvas')).toBeInTheDocument()
  })

  it('canvas has pixelated image rendering style', () => {
    const { container } = renderScene(<OfficeScene agents={[]} />)
    const canvas = container.querySelector('canvas')!
    expect(canvas.style.imageRendering).toBe('pixelated')
  })
})

describe('NeuralConstellationScene', () => {
  it('renders a canvas element', () => {
    const { container } = renderScene(<NeuralConstellationScene agents={[]} />)
    expect(container.querySelector('canvas')).toBeInTheDocument()
  })

  it('renders with agents provided', () => {
    const { container } = renderScene(<NeuralConstellationScene agents={mockAgents} />)
    expect(container.querySelector('canvas')).toBeInTheDocument()
  })

  it('renders when visible=false (kept mounted)', () => {
    const { container } = renderScene(<NeuralConstellationScene agents={[]} visible={false} />)
    expect(container.querySelector('canvas')).toBeInTheDocument()
  })

  it('canvas has pixelated image rendering style', () => {
    const { container } = renderScene(<NeuralConstellationScene agents={[]} />)
    const canvas = container.querySelector('canvas')!
    expect(canvas.style.imageRendering).toBe('pixelated')
  })
})

describe('WizardTowerScene', () => {
  it('renders a canvas element', () => {
    const { container } = renderScene(<WizardTowerScene agents={[]} />)
    expect(container.querySelector('canvas')).toBeInTheDocument()
  })

  it('renders with agents provided', () => {
    const { container } = renderScene(<WizardTowerScene agents={mockAgents} />)
    expect(container.querySelector('canvas')).toBeInTheDocument()
  })

  it('renders when visible=false (kept mounted)', () => {
    const { container } = renderScene(<WizardTowerScene agents={[]} visible={false} />)
    expect(container.querySelector('canvas')).toBeInTheDocument()
  })

  it('canvas has pixelated image rendering style', () => {
    const { container } = renderScene(<WizardTowerScene agents={[]} />)
    const canvas = container.querySelector('canvas')!
    expect(canvas.style.imageRendering).toBe('pixelated')
  })
})

describe('UnderwaterLabScene', () => {
  it('renders a canvas element', () => {
    const { container } = renderScene(<UnderwaterLabScene agents={[]} />)
    expect(container.querySelector('canvas')).toBeInTheDocument()
  })

  it('renders with agents provided', () => {
    const { container } = renderScene(<UnderwaterLabScene agents={mockAgents} />)
    expect(container.querySelector('canvas')).toBeInTheDocument()
  })

  it('renders when visible=false (kept mounted)', () => {
    const { container } = renderScene(<UnderwaterLabScene agents={[]} visible={false} />)
    expect(container.querySelector('canvas')).toBeInTheDocument()
  })

  it('canvas has pixelated image rendering style', () => {
    const { container } = renderScene(<UnderwaterLabScene agents={[]} />)
    const canvas = container.querySelector('canvas')!
    expect(canvas.style.imageRendering).toBe('pixelated')
  })
})

// MissionControlScene uses useDispatch + useNavigate, so we need providers
// (Provider/MemoryRouter/configureStore/chatReducer are already imported at the
// top of the file; Vite 8's oxc parser rejects re-importing them here as
// duplicate declarations, whereas Vite 5's esbuild tolerated it.)
import MissionControlScene from '../pages/scenes/mission-control/MissionControlScene'

function renderMC(props: { agents: AgentSource[]; visible?: boolean }) {
  const store = configureStore({ reducer: { chat: chatReducer } })
  return render(
    <Provider store={store}>
      <MemoryRouter>
        <MissionControlScene {...props} />
      </MemoryRouter>
    </Provider>
  )
}

describe('MissionControlScene', () => {
  it('renders a canvas element', () => {
    const { container } = renderMC({ agents: [] })
    expect(container.querySelector('canvas')).toBeInTheDocument()
  })

  it('renders with agents provided', () => {
    const { container } = renderMC({ agents: mockAgents })
    expect(container.querySelector('canvas')).toBeInTheDocument()
  })

  it('renders when visible=false (kept mounted)', () => {
    const { container } = renderMC({ agents: [], visible: false })
    expect(container.querySelector('canvas')).toBeInTheDocument()
  })

  it('canvas has pixelated image rendering style', () => {
    const { container } = renderMC({ agents: [] })
    const canvas = container.querySelector('canvas')!
    expect(canvas.style.imageRendering).toBe('pixelated')
  })
})


// WateringHoleScene uses useSceneInteraction (useDispatch + useNavigate) — needs providers
import WateringHoleScene from '../pages/scenes/WateringHoleScene'

function renderWH(props: { agents: AgentSource[]; visible?: boolean }) {
  const store = configureStore({ reducer: { chat: chatReducer } })
  return render(
    <Provider store={store}>
      <MemoryRouter>
        <WateringHoleScene {...props} />
      </MemoryRouter>
    </Provider>
  )
}

describe('WateringHoleScene', () => {
  it('renders a canvas element', () => {
    const { container } = renderWH({ agents: [] })
    expect(container.querySelector('canvas')).toBeInTheDocument()
  })

  it('renders with agents provided (mixed species by id)', () => {
    const animalsByKind: AgentSource[] = [
      { id: 'slot-g', name: 'Giraffe1', label: 'default', kind: 'slot', running: true, detail: '5 msgs' },
      { id: 'cron-w', name: 'Warthog1', label: 'cron', kind: 'cron', running: true, detail: 'every 5m' },
      { id: 'spawn-e', name: 'Elephant1', label: 'spawn', kind: 'spawn', running: false, detail: 'idle' },
    ]
    const { container } = renderWH({ agents: animalsByKind })
    expect(container.querySelector('canvas')).toBeInTheDocument()
  })

  it('renders when visible=false (kept mounted)', () => {
    const { container } = renderWH({ agents: [], visible: false })
    expect(container.querySelector('canvas')).toBeInTheDocument()
  })

  it('canvas has pixelated image rendering style', () => {
    const { container } = renderWH({ agents: [] })
    const canvas = container.querySelector('canvas')!
    expect(canvas.style.imageRendering).toBe('pixelated')
  })

  it('renders both pixel canvas and text overlay canvas', () => {
    const { container } = renderWH({ agents: mockAgents })
    expect(container.querySelectorAll('canvas').length).toBe(2)
  })
})
