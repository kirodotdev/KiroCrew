import { useCallback, useEffect, useState } from 'react'
import { ArrowRight } from 'lucide-react'
import { i18nT } from '../i18n/t'
import ErrorNotice from './ErrorNotice'

/**
 * First-run setup's "connect to a crew on another machine" exit.
 *
 * The desktop app's main process owns the whole switch (the crew form, stopping
 * the gateway it started, turning "Run a local gateway" off and reconnecting),
 * so this page only asks whether the exit is available and starts it. The
 * bridge is absent in plain browsers, where there is no local gateway to
 * replace, and `available` is false for any window the switch would refuse.
 */
export function useRemoteCrewSetup() {
  const [available, setAvailable] = useState(false)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    const api = window.remoteCrewSetupAPI
    if (!api) return
    let live = true
    api.available().then(
      ok => { if (live) setAvailable(ok === true) },
      () => { if (live) setAvailable(false) },
    )
    return () => { live = false }
  }, [])

  const start = useCallback(async () => {
    const api = window.remoteCrewSetupAPI
    if (!api) return
    setBusy(true)
    setFailed(false)
    try {
      const outcome = await api.start()
      // "switching" replaces this page with the crew's dashboard, so the button
      // stays disabled until it goes. A closed form changes nothing, and a
      // refused switch hides an exit that no longer works.
      if (outcome === 'switching') return
      if (outcome === 'unavailable') setAvailable(false)
      setBusy(false)
    } catch {
      setFailed(true)
      setBusy(false)
    }
  }, [])

  return { available, busy, failed, start }
}

/**
 * Renders nothing until the desktop app confirms the exit works for this
 * window, so a browser, a connection window and an adopted gateway never see
 * it. Mounted only on the first-run screen: the availability check probes the
 * port's owner, which a returning user's every load should not pay for.
 */
export default function RemoteCrewSetupSkip() {
  const { available, busy, failed, start } = useRemoteCrewSetup()
  if (!available) return null
  return (
    <div
      data-testid="remote-crew-setup-skip"
      className="mt-5 border-t border-border pt-4 text-[13px] leading-relaxed text-muted"
    >
      <span>{i18nT('components.kiroPrerequisiteGate.remote_only_question')}</span>{' '}
      <button
        type="button"
        disabled={busy}
        onClick={() => { void start() }}
        className="inline-flex cursor-pointer items-center gap-1 border-none bg-transparent p-0 font-semibold text-accent hover:underline disabled:cursor-default disabled:opacity-60 focus-ring"
      >
        {i18nT('components.kiroPrerequisiteGate.remote_only_skip')}
        <ArrowRight className="lucide-inline" />
      </button>
      {failed && (
        <div className="mt-2">
          <ErrorNotice
            variant="inline"
            message={i18nT('components.kiroPrerequisiteGate.remote_only_failed')}
            testId="remote-crew-setup-skip-error"
            noHandoffReason="this prerequisite gate hides the chat the hand-off would open, and no agent is set up on this computer to receive it"
          />
        </div>
      )}
    </div>
  )
}
