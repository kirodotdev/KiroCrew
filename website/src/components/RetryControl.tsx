import { useEffect, useRef } from 'react'
import { RefreshCw } from 'lucide-react'
import { Btn } from './ui'

/**
 * The Retry affordance a failure notice pairs with: it owns the `aria-busy` pair, the
 * "<label>: <cause>" accessible name, and returning focus after a failed attempt.
 */
export function RetryControl({ busy, cause, label, onRetry }: {
  /** True while THIS control's own retry is in flight. */
  busy: boolean
  /** The notice text, which becomes the accessible name's second half. */
  cause: string
  label: string
  /** Fired on click; the caller owns `busy` for as long as its read is outstanding. */
  onRetry: () => void | Promise<unknown>
}) {
  const ref = useRef<HTMLButtonElement | null>(null)
  const wanted = useRef(false)

  useEffect(() => {
    if (busy || !wanted.current) return
    wanted.current = false
    ref.current?.focus()
  }, [busy])

  return (
    <Btn
      ref={r => { ref.current = r }}
      type="button"
      // Keeps the field behind the notice focused: without it the mousedown blurs the input
      // before the click lands, so a successful retry has nowhere to hand focus back to.
      onMouseDown={e => e.preventDefault()}
      onClick={() => {
        // A disabled button cannot hold focus, so a failed attempt would cost a Tab per retry.
        wanted.current = true
        void onRetry()
      }}
      disabled={busy}
      aria-busy={busy}
      aria-label={`${label}: ${cause}`}
      className="shrink-0 text-[11px] px-1.5 py-0.5 rounded"
    >
      <RefreshCw
        size={10}
        className={busy ? 'animate-spin inline-block mr-1' : 'invisible inline-block mr-1'}
      />
      {label}
    </Btn>
  )
}
