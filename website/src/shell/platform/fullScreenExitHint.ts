import { useEffect } from 'react'
import { useAppDispatch, useAppStore } from '../../store'
import { setAgentSwitchNotice } from '../../store/chatSlice'
import { i18nT } from '../../i18n/t'
import { subscribeFullScreenExitHint } from './electronBridge'

/**
 * Tell a Windows or Linux desktop user how to leave fullscreen. There it hides
 * the caption buttons and the menu bar, so after F11 nothing on screen says
 * how to get them back. The hint uses the shell's shared notice banner, which
 * the user can dismiss. It never replaces a notice already shown, and leaving
 * fullscreen clears it when it is still the one shown.
 */
export function useFullScreenExitHint(): void {
  const dispatch = useAppDispatch()
  const store = useAppStore()
  useEffect(() => {
    let shown: unknown = null
    const unsubscribe = subscribeFullScreenExitHint(
      () => {
        // A notice already up can carry an offer (a new chat, an open
        // session) that the hint would erase for good; it wins.
        if (store.getState().chat.agentSwitchNotice !== null) return
        dispatch(setAgentSwitchNotice(i18nT('app.fullscreen_exit_hint')))
        shown = store.getState().chat.agentSwitchNotice
      },
      () => {
        if (shown !== null && store.getState().chat.agentSwitchNotice === shown) dispatch(setAgentSwitchNotice(null))
        shown = null
      },
    )
    return unsubscribe
  }, [dispatch, store])
}
