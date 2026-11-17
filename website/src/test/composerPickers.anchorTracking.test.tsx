import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useRef, useState, useEffect, type ReactNode, type RefObject } from 'react'

const mockApi = vi.hoisted(() => ({ fileSearch: vi.fn(), slashCommands: vi.fn(), skills: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))

import FilePickerMenu from '../components/FilePickerMenu'
import SlashCommandMenu from '../components/SlashCommandMenu'
import SkillPickerMenu from '../components/SkillPickerMenu'

/**
 * The composer pickers read the composer's rect only while they render. When
 * the docked terminal closes, it unmounts after its slide-out and the composer
 * drops by the panel's height with no window resize or scroll, so a picker
 * left open kept its old, higher position. These tests move the anchor and
 * fire only a ResizeObserver callback (what an ancestor growing produces), and
 * expect the menu's bottom edge to follow.
 */

class FakeResizeObserver {
  static instances: FakeResizeObserver[] = []
  constructor(private cb: ResizeObserverCallback) { FakeResizeObserver.instances.push(this) }
  observe() {}
  unobserve() {}
  disconnect() {}
  fire() { this.cb([], this as unknown as ResizeObserver) }
}

const originalRO = window.ResizeObserver
beforeEach(() => {
  vi.clearAllMocks()
  FakeResizeObserver.instances = []
  window.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver
})
afterEach(() => {
  window.ResizeObserver = originalRO
  vi.restoreAllMocks()
})

const anchor = { top: 500 }
const rectAt = (top: number) => ({
  top, left: 40, width: 600, height: 44, bottom: top + 44,
  right: 640, x: 40, y: top, toJSON: () => ({}),
}) as DOMRect

function Harness({ menu }: { menu: (ref: RefObject<HTMLDivElement | null>) => ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)
  const [qc] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }))
  const [, tick] = useState(0)
  useEffect(() => {
    if (ref.current) ref.current.getBoundingClientRect = () => rectAt(anchor.top)
    tick(1)
  }, [])
  return (
    <QueryClientProvider client={qc}>
      <div ref={ref}>anchor</div>
      {menu(ref)}
    </QueryClientProvider>
  )
}

const neverSettles = () => new Promise<never>(() => {})

/** Move the anchor down 200px with no window event, as a terminal closing does. */
function moveAnchorDown() {
  anchor.top = 700
  act(() => {
    // Real observers report initial sizes first; the hook ignores that one.
    for (const ro of FakeResizeObserver.instances) { ro.fire(); ro.fire() }
  })
}

const positioned = (el: HTMLElement) => el.closest('div[style]') as HTMLElement

describe('composer pickers follow the composer when it moves', () => {
  beforeEach(() => { anchor.top = 500 })

  it('FilePickerMenu', async () => {
    mockApi.fileSearch.mockImplementation(neverSettles)
    render(<Harness menu={ref => <FilePickerMenu query="z" anchorRef={ref} open onSelect={vi.fn()} onClose={vi.fn()} />} />)
    const menu = await screen.findByRole('listbox')
    expect(menu.style.bottom).toBe(`${window.innerHeight - 500 + 4}px`)
    moveAnchorDown()
    expect(screen.getByRole('listbox').style.bottom).toBe(`${window.innerHeight - 700 + 4}px`)
  })

  it('SlashCommandMenu', async () => {
    mockApi.slashCommands.mockImplementation(neverSettles)
    render(<Harness menu={ref => <SlashCommandMenu input="/zzq" anchorRef={ref} open onSelect={vi.fn()} onClose={vi.fn()} />} />)
    const menu = await screen.findByRole('listbox')
    expect(menu.style.bottom).toBe(`${window.innerHeight - 500 + 4}px`)
    moveAnchorDown()
    expect(screen.getByRole('listbox').style.bottom).toBe(`${window.innerHeight - 700 + 4}px`)
  })

  it('SkillPickerMenu', async () => {
    mockApi.skills.mockImplementation(neverSettles)
    render(<Harness menu={ref => <SkillPickerMenu query="" anchorRef={ref} open onSelect={vi.fn()} onClose={vi.fn()} />} />)
    const menu = positioned(await screen.findByRole('status'))
    expect(menu.style.bottom).toBe(`${window.innerHeight - 500 + 4}px`)
    moveAnchorDown()
    expect(positioned(screen.getByRole('status')).style.bottom).toBe(`${window.innerHeight - 700 + 4}px`)
  })
})
