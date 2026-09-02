// ColumnSplitter is the W3C APG "window splitter": role=separator with value
// semantics plus keyboard nudging. Covers the arrow-key branches, the ignored
// key, and that the pointer-drag handlers are spread onto the element.
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { usePointerDrag } from '../hooks/usePointerDrag'
import ColumnSplitter from '../apps/spec-builder/components/ColumnSplitter'

function handleProps(overrides: Partial<ReturnType<typeof usePointerDrag>> = {}) {
  return {
    onPointerDown: vi.fn(),
    onPointerMove: vi.fn(),
    onPointerUp: vi.fn(),
    onPointerCancel: vi.fn(),
    ...overrides,
  } as ReturnType<typeof usePointerDrag>
}

function renderSplitter(onNudge = vi.fn(), props = handleProps()) {
  render(
    <ColumnSplitter
      handleProps={props}
      label="zz-rail-edge"
      valueNow={41.6}
      valueMin={20}
      valueMax={60}
      onNudge={onNudge}
    />,
  )
  return { onNudge, props, sep: screen.getByRole('separator') }
}

describe('ColumnSplitter', () => {
  it('exposes focusable separator semantics with a rounded value', () => {
    const { sep } = renderSplitter()
    expect(sep).toHaveAttribute('aria-orientation', 'vertical')
    expect(sep).toHaveAttribute('aria-label', 'zz-rail-edge')
    expect(sep).toHaveAttribute('aria-valuenow', '42')
    expect(sep).toHaveAttribute('aria-valuemin', '20')
    expect(sep).toHaveAttribute('aria-valuemax', '60')
    expect(sep).toHaveAttribute('tabindex', '0')
  })

  it('nudges left on ArrowLeft', () => {
    const { sep, onNudge } = renderSplitter()
    fireEvent.keyDown(sep, { key: 'ArrowLeft' })
    expect(onNudge).toHaveBeenCalledWith(-1)
  })

  it('nudges right on ArrowRight', () => {
    const { sep, onNudge } = renderSplitter()
    fireEvent.keyDown(sep, { key: 'ArrowRight' })
    expect(onNudge).toHaveBeenCalledWith(1)
  })

  it('ignores keys outside the horizontal pair', () => {
    const { sep, onNudge } = renderSplitter()
    fireEvent.keyDown(sep, { key: 'ArrowUp' })
    fireEvent.keyDown(sep, { key: 'Enter' })
    expect(onNudge).not.toHaveBeenCalled()
  })

  it('spreads the pointer-drag handlers onto the handle', () => {
    const props = handleProps()
    const { sep } = renderSplitter(vi.fn(), props)
    fireEvent.pointerDown(sep)
    expect(props.onPointerDown).toHaveBeenCalled()
  })
})
