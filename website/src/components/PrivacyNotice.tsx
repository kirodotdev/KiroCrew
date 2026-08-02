import { ShieldCheck } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useEffect, useRef, useState } from 'react'
import { Btn } from './ui'
import { i18nT } from '../i18n/t'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'

export const PRIVACY_NOTICE_STORAGE_KEY = 'mc-privacy-notice-v1'

/**
 * Passive first-run disclosure for the anonymous usage beacon.
 *
 * This is deliberately a normal-flow region, not a dialog: it never traps
 * focus, covers content, or gates app use. Dismissal is best-effort persisted;
 * storage failures cannot keep the current session's notice open.
 */
export default function PrivacyNotice() {
  const [visible, setVisible] = useState(
    () => safeGetItem(PRIVACY_NOTICE_STORAGE_KEY) !== '1',
  )
  const dismissedByUser = useRef(false)

  useEffect(() => {
    if (visible || !dismissedByUser.current) return
    document.getElementById('main-content')?.focus()
  }, [visible])

  if (!visible) return null

  const dismiss = () => {
    dismissedByUser.current = true
    safeSetItem(PRIVACY_NOTICE_STORAGE_KEY, '1')
    setVisible(false)
  }

  return (
    <section
      aria-labelledby="privacy-notice-title"
      aria-describedby="privacy-notice-description"
      className="shrink-0 mx-3 mt-3 rounded-lg border border-accent/30 bg-accent-subtle px-4 py-3 flex items-start gap-3 animate-rise"
      data-testid="privacy-notice"
    >
      <ShieldCheck className="lucide-inline text-accent shrink-0 mt-0.5" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <h2 id="privacy-notice-title" className="text-[13px] font-semibold text-text-strong">
          {i18nT('privacyDisclosure.noticeTitle')}
        </h2>
        <p id="privacy-notice-description" className="text-[12px] leading-relaxed text-muted mt-0.5">
          {i18nT('privacyDisclosure.noticeBody')}
        </p>
      </div>
      <div className="shrink-0 flex items-center gap-2 max-[640px]:flex-col max-[640px]:items-end">
        <Link
          to="/settings?tab=privacy"
          className="text-[13px] font-medium text-accent hover:underline rounded focus-ring"
        >
          {i18nT('privacyDisclosure.details')}
        </Link>
        <Btn type="button" onClick={dismiss}>
          {i18nT('app.dismiss')}
        </Btn>
      </div>
    </section>
  )
}
