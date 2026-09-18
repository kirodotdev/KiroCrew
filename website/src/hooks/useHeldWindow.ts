import { useEffect, useRef, useState } from 'react'

/**
 * A countdown that can be HELD: the window for a time-limited offer (an undo),
 * suspended while the user is on it and resumed with its remainder when they
 * leave, so the deadline never expires under a hand that is reaching for the
 * offer. Owned once, here, for every surface that offers an undo -- the
 * session-move bar ({@link useMoveUndo}) and the header's Auto-title Undo
 * ({@link SessionTitleControl}) -- so the product has one clock.
 *
 * `key` identifies the CURRENT offer (any value; `null` = no offer). A new key
 * starts a full window, whatever the previous one had spent, and a hold that is
 * already in force when a new key arrives holds that full window rather than a
 * remainder read off the previous offer's deadline -- the pointer is usually
 * parked on the control that just created the offer. `held` is the caller's
 * hold signal, already keyed however the caller keys it. `onExpire` fires once
 * when the window runs out; the caller clears its offer there.
 *
 * `remainingMs` is what a progress indicator draws while paused: the remainder
 * frozen when the hold began, the full window while running or for a fresh key.
 */
export default function useHeldWindow<K>(
  key: K | null,
  windowMs: number,
  held: boolean,
  onExpire: () => void,
): { remainingMs: number; paused: boolean } {
  const [spent, setSpent] = useState<{ key: K; remaining: number } | null>(null)
  const paused = key != null && held
  const remainingMs = key != null && spent?.key === key ? spent.remaining : windowMs
  // The deadline and the key it was set for: a remainder is read off the
  // deadline ONLY when it belongs to this key.
  const deadline = useRef<{ key: K | null; at: number }>({ key: null, at: 0 })
  const onExpireRef = useRef(onExpire)
  onExpireRef.current = onExpire
  useEffect(() => {
    if (key == null) return
    if (paused) {
      setSpent({
        key,
        remaining: deadline.current.key === key ? Math.max(0, deadline.current.at - Date.now()) : windowMs,
      })
      return
    }
    deadline.current = { key, at: Date.now() + remainingMs }
    const timer = setTimeout(() => onExpireRef.current(), remainingMs)
    return () => clearTimeout(timer)
    // Keyed on the offer and the hold ALONE: the remainder this effect writes
    // when it freezes is the input to the NEXT resume, not a new window, and
    // `windowMs` is a constant at every call site.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, paused])
  return { remainingMs, paused }
}
