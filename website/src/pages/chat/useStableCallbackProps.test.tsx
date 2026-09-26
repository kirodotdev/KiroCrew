import { describe, it, expect } from 'vitest'
import { memo } from 'react'
import { render } from '@testing-library/react'
import { useStableCallbackProps } from './useStableCallbackProps'

type ChildProps = { label: string; onPick?: (n: number) => string }

describe('useStableCallbackProps', () => {
  it('keeps a memoized child from re-rendering on fresh inline callbacks, and calls the latest one', () => {
    let childRenders = 0
    let received: ChildProps | null = null
    const Child = memo(function Child(props: ChildProps) {
      childRenders++
      received = props
      return null
    })
    function Host({ label, tag }: { label: string; tag: string }) {
      return <Child {...useStableCallbackProps<ChildProps>({ label, onPick: n => `${tag}:${n}` })} />
    }
    const { rerender } = render(<Host label="a" tag="first" />)
    const firstOnPick = received!.onPick
    rerender(<Host label="a" tag="second" />)
    // A new inline arrow on every host render, yet the child bailed out...
    expect(childRenders).toBe(1)
    expect(received!.onPick).toBe(firstOnPick)
    // ...and the forwarder runs the host's latest closure, not the first one.
    expect(received!.onPick!(7)).toBe('second:7')
    rerender(<Host label="b" tag="second" />)
    expect(childRenders).toBe(2)
    expect(received!.onPick).toBe(firstOnPick)
  })

  it('keeps an absent callback absent', () => {
    let seen: ChildProps | null = null
    function Child(props: ChildProps) { seen = props; return null }
    function Host({ withHandler }: { withHandler: boolean }) {
      const props = useStableCallbackProps<ChildProps>({ label: 'x', onPick: withHandler ? n => String(n) : undefined })
      return <Child {...props} />
    }
    const { rerender } = render(<Host withHandler={false} />)
    expect(seen!.onPick).toBeUndefined()
    rerender(<Host withHandler />)
    expect(seen!.onPick!(3)).toBe('3')
  })
})
