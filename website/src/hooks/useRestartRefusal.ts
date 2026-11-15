import { useCallback, useContext, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { ReactReduxContext } from 'react-redux'
import type { RootState } from '../store'

const noSubscription = () => () => {}

/**
 * Hear a restart the gateway refuses after `POST /api/restart` answered 200.
 *
 * That 200 only means the restart was handed to its worker. The worker can still
 * refuse it (the entry point is gone, sessions would not drain), and that verdict
 * arrives only as an `error` or `failed` update-progress step. Call `begin` when
 * the restart is sent and `end` when it is answered with a failure; `onRefused`
 * gets the step's detail.
 *
 * The store keeps the last attempt's terminal step, so a refusal counts only
 * after this attempt's own `restarting` step has been seen. Without the
 * dashboard store (an embed), nothing is reported.
 */
export function useRestartRefusal(onRefused: (detail: string) => void) {
  const store = useContext(ReactReduxContext)?.store
  const progress = useSyncExternalStore(
    store ? store.subscribe : noSubscription,
    () => (store ? (store.getState() as RootState).dashboard.updateProgress : null),
  )
  const step = progress?.step ?? ''
  const detail = progress?.detail ?? ''
  const [active, setActive] = useState(false)
  const sawRestartingRef = useRef(false)
  const onRefusedRef = useRef(onRefused)
  useEffect(() => { onRefusedRef.current = onRefused })

  const begin = useCallback(() => {
    sawRestartingRef.current = false
    setActive(true)
  }, [])
  const end = useCallback(() => setActive(false), [])

  useEffect(() => {
    if (!active) return
    if (step === 'restarting') {
      sawRestartingRef.current = true
      return
    }
    if (step !== 'error' && step !== 'failed') return
    if (!sawRestartingRef.current) return
    setActive(false)
    onRefusedRef.current(detail)
  }, [active, step, detail])

  return { begin, end }
}
