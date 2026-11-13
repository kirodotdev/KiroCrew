import { useCallback, useEffect, useState } from 'react'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { RestartGatewayButton } from './AboutPanelUpdateFlow'

/**
 * Settings → About's Restart gateway in the desktop app.
 *
 * The app restarts the gateway it runs (stop, respawn, reconnect) instead of
 * this page calling `POST /api/restart`: that endpoint replaces the process in
 * place, which the app's supervisor cannot follow (see
 * electron/runtime/gateway/user-restart.js). So the control is shown only where
 * the app says its restart works: the main window, connected to a gateway this
 * app started. A remote crew, an adopted gateway and a browser tab never see it.
 *
 * `available` is read by the panel too: where it is true, the gateway-side
 * restart controls stand down so the card shows one.
 */
export function useDesktopGatewayRestart() {
  const [available, setAvailable] = useState(false)
  const [pending, setPending] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const api = window.gatewayRestartAPI
    if (!api) return
    let live = true
    api.available().then(
      ok => { if (live) setAvailable(ok === true) },
      () => { if (live) setAvailable(false) },
    )
    return () => { live = false }
  }, [])

  const restart = useCallback(async () => {
    const api = window.gatewayRestartAPI
    if (!api) return
    setPending(true)
    setError('')
    try {
      // "restarting" replaces this page with the app's splash, so the button
      // stays busy until the page goes.
      if ((await api.restart()) === 'restarting') {
        setRestarting(true)
        return
      }
      setError(i18nT('pages.settings.aboutPanel.desktop_restart_unavailable'))
    } catch {
      setError(i18nT('pages.settings.aboutPanel.desktop_restart_unavailable'))
    } finally {
      setPending(false)
    }
  }, [])

  return { available, pending, restarting, error, restart }
}

export function DesktopGatewayRestartRow({ state }: { state: ReturnType<typeof useDesktopGatewayRestart> }) {
  const { available, pending, restarting, error, restart } = state
  if (!available) return null
  return (
    <>
      <div className="flex items-center justify-between gap-3 mt-2.5 pt-2.5 border-t border-border"
        data-testid="desktop-gateway-restart-row">
        <span className="text-sm text-muted">{i18nT('pages.settings.aboutPanel.desktop_restart_row_note')}</span>
        <div className="shrink-0">
          <RestartGatewayButton
            pending={pending}
            restarting={restarting}
            onConfirm={() => { void restart() }}
            testId="gateway-restart-desktop"
          />
        </div>
      </div>
      {/* askAgent on, as on the browser row: nothing on this card is a draft. */}
      <ErrorNotice message={error} askAgent testId="desktop-gateway-restart-error" />
    </>
  )
}
