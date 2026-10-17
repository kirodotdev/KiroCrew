/**
 * What the app-level Kiro sign-in notice's error boundary shows when the
 * notice itself fails: its chunk would not load, or it threw while rendering.
 *
 * It lives outside the notice's lazy chunk, since a chunk that failed to load
 * cannot draw its own fallback. The title makes no sign-in claim: the notice
 * is mounted on every backend, and only KAS has a Kiro sign-in to check. The
 * error text stays as the secondary line because the hand-off matches the
 * journaled report by it. Dismiss hides it until the page reloads.
 */
import { useState } from 'react'

import { i18nT } from '../i18n/t'
import ErrorNotice from './ErrorNotice'

export default function KiroSignInNoticeFallback({ error }: { error: Error }) {
  const [dismissed, setDismissed] = useState(false)
  if (dismissed) return null
  return (
    <ErrorNotice
      className="mx-3 md:mx-6 mt-4 mb-2"
      title={i18nT('components.kiroSignInNotice.load_failed')}
      message={error.message || i18nT('components.kiroSignInNotice.load_failed')}
      messagePlacement="below"
      actionPlacement="below"
      // The notice holds no input, and the hand-off asks the page's leave
      // guard before navigating, so nothing on the page below is lost.
      askAgent
      onDismiss={() => setDismissed(true)}
      testId="kiro-sign-in-notice-load-error"
    />
  )
}
