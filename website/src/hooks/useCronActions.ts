/** Shared cron job actions (run, open-in-chat) used by CronTab and SchedulePage */
import { useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'

export function useCronActions(load: () => void) {
  const navigate = useNavigate()
  const [running, setRunning] = useState<Set<string>>(new Set())
  const [actionError, setActionError] = useState<{ id: string; msg: string } | null>(null)

  const runNow = useCallback(async (id: string) => {
    setRunning(prev => new Set(prev).add(id)); setActionError(null)
    try {
      const res = await api.runCron(id)
      if (res.error) { setActionError({ id, msg: res.error }); return }
      load()
    } catch (e: unknown) {
      setActionError({ id, msg: e instanceof Error ? e.message : 'Failed to run job' })
    } finally { setRunning(prev => { const s = new Set(prev); s.delete(id); return s }) }
  }, [load])

  const [cancelling, setCancelling] = useState<Set<string>>(new Set())
  const cancelRun = useCallback(async (id: string) => {
    setCancelling(prev => new Set(prev).add(id)); setActionError(null)
    try {
      const res = await api.cancelCron(id)
      if (res.error) { setActionError({ id, msg: res.error }); return }
      load()
    } catch (e: unknown) {
      setActionError({ id, msg: e instanceof Error ? e.message : 'Failed to cancel job' })
    } finally { setCancelling(prev => { const s = new Set(prev); s.delete(id); return s }) }
  }, [load])

  const openInChat = useCallback(async (id: string) => {
    setActionError(null)
    try {
      const res = await api.cronToChat(id)
      if (res.error) { setActionError({ id, msg: res.error }); return }
      if (res.slot) navigate('/chat?slot=' + res.slot)
    } catch { setActionError({ id, msg: 'Failed to open in chat' }) }
  }, [navigate])

  return { running, setRunning, actionError, setActionError, runNow, openInChat, cancelling, cancelRun }
}
