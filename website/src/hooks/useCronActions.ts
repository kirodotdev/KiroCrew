/** Shared cron job actions (run, open-in-chat) used by SchedulePage */
import { useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'

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
      setActionError({ id, msg: e instanceof Error ? e.message : i18nT('hooks.useCronActions.failed_to_run_job') })
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
      setActionError({ id, msg: e instanceof Error ? e.message : i18nT('hooks.useCronActions.failed_to_cancel_job') })
    } finally { setCancelling(prev => { const s = new Set(prev); s.delete(id); return s }) }
  }, [load])

  const toggleEnabled = useCallback(async (id: string, enabled: boolean) => {
    setActionError(null)
    try {
      const res = await api.toggleCron(id, enabled)
      if (res?.error) { setActionError({ id, msg: res.error }); return }
      load()
    } catch (e: unknown) {
      // Name the action the user actually took: a pause that failed must not
      // report that a RUN failed, which is the opposite of their intent.
      const fallback = enabled
        ? i18nT('hooks.useCronActions.failed_to_resume_job')
        : i18nT('hooks.useCronActions.failed_to_pause_job')
      setActionError({ id, msg: e instanceof Error ? e.message : fallback })
    }
  }, [load])

  const openInChat = useCallback(async (id: string) => {
    setActionError(null)
    try {
      const res = await api.cronToChat(id)
      if (res.error) { setActionError({ id, msg: res.error }); return }
      if (res.slot) navigate('/chat?slot=' + res.slot)
    } catch { setActionError({ id, msg: i18nT('hooks.useCronActions.failed_to_open_in_chat') }) }
  }, [navigate])

  return { running, setRunning, actionError, setActionError, runNow, toggleEnabled, openInChat, cancelling, cancelRun }
}
