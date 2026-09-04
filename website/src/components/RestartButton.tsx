import { useState } from 'react'
import { Zap } from 'lucide-react'
import { api } from '../api/client'

import { i18nT } from '../i18n/t'
export default function RestartButton() {
  const [restarting, setRestarting] = useState(false)
  const [msg, setMsg] = useState('')
  const [isError, setIsError] = useState(false)

  const restart = async () => {
    setRestarting(true)
    try {
      const res = await api.restartSessions()
      // The sessions DID restart, but a failed reconcile means they restarted
      // against a config that may not match the sources — reporting "config
      // applied" there would be the exact lie this button exists to avoid.
      if (res && res.mcp_sync_ok === false) {
        setIsError(true)
        setMsg(i18nT('components.restartButton.sessions_restarted_but_mcp_sync_failed'))
      } else {
        setIsError(false)
        setMsg(i18nT('components.restartButton.sessions_restarted_config_applied'))
      }
    } catch (e: unknown) {
      setIsError(true)
      setMsg(e instanceof Error ? e.message : i18nT('components.restartButton.restart_failed'))
    } finally {
      setRestarting(false)
      setTimeout(() => setMsg(''), 5000)
    }
  }

  return (
    <div className="flex items-center gap-2">
      {msg && <span className={`text-[13px] animate-rise ${isError ? 'text-danger' : 'text-ok'}`}>{msg}</span>}
      <button
        onClick={restart}
        disabled={restarting}
        className={`group relative inline-flex items-center gap-1.5 px-4 py-1.5 rounded-lg text-[13px] font-semibold font-body cursor-pointer transition-all duration-300 overflow-hidden border-none ${
          restarting
            ? 'bg-accent/60 text-accent-fg/80 cursor-wait'
            : 'bg-gradient-to-r from-accent to-accent-hover text-accent-fg shadow-[0_2px_8px_var(--accent-glow)] hover:shadow-[0_4px_20px_var(--accent-glow)] hover:-translate-y-0.5 active:translate-y-0'
        }`}
      >
        {restarting && <span className="absolute inset-0 bg-gradient-to-r from-transparent via-white/20 to-transparent animate-shimmer" />}
        <span className={`transition-transform duration-300 ${restarting ? 'animate-spin' : 'group-hover:rotate-12'}`}><Zap className="lucide-inline" /></span>
        {restarting
          ? <span>{i18nT('components.restartButton.restarting')}</span>
          : <span>{i18nT('components.restartButton.apply_restart')}</span>
        }
      </button>
    </div>
  )
}
