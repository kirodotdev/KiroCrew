import { useCallback, useRef, useState } from 'react'

/** One submission owns both its confirmation and its mutation until settlement. */
export function useGuardedMutation(mutate: () => Promise<unknown>, confirm: () => Promise<boolean>) {
  const pending = useRef(false)
  const [isPending, setIsPending] = useState(false)
  const run = useCallback(async () => {
    if (pending.current) return
    pending.current = true
    setIsPending(true)
    try {
      if (await confirm()) {
        try {
          await mutate()
        } catch {
          // The mutation's ErrorNotice owns its failure; another click may retry.
        }
      }
    } finally {
      pending.current = false
      setIsPending(false)
    }
  }, [confirm, mutate])
  return { run, isPending }
}
