// Spec Builder's skeleton primitives: every exported placeholder renders, the
// announced status lives OUTSIDE the aria-hidden subtree, and ShimmerLine
// applies its geometry + animation-delay props.
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import {
  ShimmerLine,
  SpecListSkeleton,
  DocSkeleton,
  ChatColumnSkeleton,
} from '../apps/spec-builder/components/Shimmer'

describe('ShimmerLine', () => {
  it('applies the requested width/height and the shimmer delay', () => {
    const { container } = render(<ShimmerLine w="42%" h="9px" delay={0.5} />)
    const base = container.firstElementChild as HTMLElement
    expect(base.style.width).toBe('42%')
    expect(base.style.height).toBe('9px')
    const sweep = base.querySelector('.animate-shimmer') as HTMLElement
    expect(sweep).toBeTruthy()
    expect(sweep.style.animationDelay).toBe('0.5s')
  })

  it('defaults the height and the delay', () => {
    const { container } = render(<ShimmerLine w="10px" />)
    const base = container.firstElementChild as HTMLElement
    expect(base.style.height).toBe('12px')
    expect((base.querySelector('.animate-shimmer') as HTMLElement).style.animationDelay).toBe('0s')
  })
})

describe('SpecListSkeleton', () => {
  it('announces loading outside the aria-hidden subtree', () => {
    const { container } = render(<SpecListSkeleton />)
    const status = screen.getByRole('status')
    expect(status).toBeInTheDocument()
    expect(status.closest('[aria-hidden="true"]')).toBeNull()
    expect(container.querySelector('[aria-hidden="true"]')).toBeTruthy()
  })

  it('renders one placeholder row per count, two bars each', () => {
    const { container } = render(<SpecListSkeleton count={3} />)
    const rows = container.querySelectorAll('[aria-hidden="true"] > div')
    expect(rows).toHaveLength(3)
    expect(container.querySelectorAll('.animate-shimmer')).toHaveLength(6)
  })

  it('defaults to four rows', () => {
    const { container } = render(<SpecListSkeleton />)
    expect(container.querySelectorAll('[aria-hidden="true"] > div')).toHaveLength(4)
  })

  it('cycles the row widths so a stack is not uniform', () => {
    const { container } = render(<SpecListSkeleton count={2} />)
    const widths = [...container.querySelectorAll('[aria-hidden="true"] > div')].map(
      row => (row.children[1] as HTMLElement).style.width,
    )
    expect(widths[0]).not.toBe(widths[1])
  })
})

describe('DocSkeleton', () => {
  it('announces its status and shapes prose bars behind aria-hidden', () => {
    const { container } = render(<DocSkeleton />)
    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(screen.getByRole('status').closest('[aria-hidden="true"]')).toBeNull()
    // heading + 3 paragraph lines + sub-heading + 3 bulleted rows (dot + text)
    expect(container.querySelectorAll('.animate-shimmer')).toHaveLength(11)
  })
})

describe('ChatColumnSkeleton', () => {
  it('announces its status and renders three bubble rows', () => {
    const { container } = render(<ChatColumnSkeleton />)
    expect(screen.getByRole('status')).toBeInTheDocument()
    const rows = container.querySelectorAll('[aria-hidden="true"] > div')
    expect(rows).toHaveLength(3)
    expect(rows[0].className).toContain('justify-start')
    expect(rows[1].className).toContain('justify-end')
    expect(container.querySelectorAll('.animate-shimmer')).toHaveLength(6)
  })
})
