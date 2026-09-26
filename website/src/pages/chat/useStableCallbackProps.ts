import { useRef } from 'react'

/**
 * Returns `props` with every function-valued prop replaced by a forwarder whose
 * identity never changes and which calls the LATEST function passed under that
 * name. Non-function props pass through untouched.
 *
 * For a memoized child whose host rebuilds its callbacks every render (inline
 * arrows over the host's state): the child's `memo` then compares only the
 * data props, so a host render that changed nothing the child shows does not
 * re-render it, and a call through a forwarder never runs a stale closure.
 *
 * The latest props are recorded during render, before the child renders, so a
 * forwarder called from the child's own render or effects already sees them.
 * Presence is preserved: a prop that is `undefined` stays `undefined`, so a
 * child that branches on "was a handler passed" behaves as before.
 *
 * Page-local: ChatPage's composer is the one host that builds its callbacks
 * inline every render. Move it to hooks/ when a second host needs it.
 *
 * Not for component-typed props (`icon={SomeComponent}`): a forwarder is a new
 * function, so React would see a different component type.
 */
export function useStableCallbackProps<P extends object>(props: P): P {
  const latest = useRef(props)
  latest.current = props
  const forwarders = useRef(new Map<string, (...args: unknown[]) => unknown>())
  const out: Record<string, unknown> = {}
  for (const [name, value] of Object.entries(props)) {
    if (typeof value !== 'function') { out[name] = value; continue }
    let forward = forwarders.current.get(name)
    if (!forward) {
      forward = (...args: unknown[]) => {
        const fn = (latest.current as Record<string, unknown>)[name]
        return typeof fn === 'function' ? fn(...args) : undefined
      }
      forwarders.current.set(name, forward)
    }
    out[name] = forward
  }
  return out as P
}
