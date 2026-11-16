import { useEffect, useState } from 'react'
import { QUIET_RECONNECT_GRACE_MS } from '../utils/reconnectBackoff'
import { clearGatewayRestart, gatewayRestartPendingMs } from '../lib/gatewayRestart'
import type { OfflineCover } from '../utils/offlineReason'

/** `offlineCover`'s answer, with the host's "not running" view held back until
 *  the drop has outlasted the relay path's quiet-reconnect grace, and for as
 *  long as a restart this tab asked for is pending: a short blip or the
 *  gateway's own re-exec is not a gateway that stopped. The relay cover is
 *  passed through as it is. */
export function useHostOfflineCover(cover: OfflineCover, connected: boolean): OfflineCover {
  const [graceOver, setGraceOver] = useState(false)
  useEffect(() => { if (connected) clearGatewayRestart() }, [connected])
  useEffect(() => {
    if (cover !== 'host') return
    let timer: ReturnType<typeof setTimeout>
    const check = () => {
      const pending = gatewayRestartPendingMs()
      if (pending > 0) { timer = setTimeout(check, pending); return }
      setGraceOver(true)
    }
    timer = setTimeout(check, QUIET_RECONNECT_GRACE_MS)
    return () => { clearTimeout(timer); setGraceOver(false) }
  }, [cover])
  return cover === 'host' && !graceOver ? null : cover
}
