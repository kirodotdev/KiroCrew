import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Copy, Smartphone } from 'lucide-react'
import { api } from '../../api/client'
import { Btn, Card, CardTitle, Input } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { useAppSelector } from '../../store'
import { parseErrorCode } from '../../utils/errorReport'
import { copyToClipboard } from '../../utils/clipboard'

const mobileLinkErrorCode = (error: unknown): string | undefined =>
  typeof error === 'object' && error !== null && 'body' in error
    ? parseErrorCode(typeof error.body === 'string' ? error.body : undefined)
    : undefined

/**
 * Handler error code → catalog key, written out in full.
 *
 * `api_auth_mobile_link` refuses a mint with seven distinct codes, and three of
 * them need copy that names an action retrying cannot supply: two are 403s the
 * caller can only clear by changing session (`restricted_session`,
 * `caller_session_expired`), and one is a configuration gap
 * (`external_origin_unavailable`). Everything else — `bad_origin`,
 * `unauthenticated`, `app_token_forbidden`, `governance_denied` — is either
 * transient or an operator decision the card cannot coach, so it keeps the
 * generic retry copy.
 *
 * Each key is a plain string literal in an `as const` map rather than a key
 * assembled at the call site: a constructed key is invisible to every static
 * tool, so the keys would read as dead and a pruning pass would delete them —
 * see `src/i18n/dynamicKeys.test.ts`, and `AboutPanel`'s `UPDATE_ERROR_KEYS`
 * for the same shape.
 */
const MOBILE_LINK_ERROR_KEYS = {
  external_origin_unavailable:
    'pages.settings.mobileLoginCard.dashboard_url_required_to_create_a_mobile_sign_in_link',
  restricted_session:
    'pages.settings.mobileLoginCard.restricted_sessions_cannot_create_a_sign_in_link',
  caller_session_expired:
    'pages.settings.mobileLoginCard.session_expired_sign_in_again_to_create_a_link',
} as const

export function MobileLoginCard() {
  const { t } = useTranslation()
  const [link, setLink] = useState('')
  const [expiresIn, setExpiresIn] = useState<number | null>(null)
  const [copied, setCopied] = useState(false)
  const [copyFailed, setCopyFailed] = useState(false)
  // The active slot's key rides the request so the server's restricted-session
  // guard sees the REAL session, not the shared `dashboard:ui` default (which
  // it treats as unrestricted). Without this an incognito/temporary slot could
  // mint a durable credential the operator deliberately withheld from it.
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const sessionKey = activeSlot ? `dashboard:${activeSlot}` : undefined

  const createLink = useMutation({
    mutationFn: () => api.mobileLoginLink(sessionKey),
    onMutate: () => {
      setCopied(false)
      setCopyFailed(false)
    },
    onSuccess: result => {
      setLink(result.url)
      setExpiresIn(result.expires_in)
    },
  })

  const copyLink = async () => {
    if (!link) return
    // False = the fallback reported failure; a throw = both paths dead.
    // Either way the user needs the manual-copy hint, not a false tick.
    let ok = false
    try {
      ok = await copyToClipboard(link)
    } catch {
      ok = false
    }
    setCopied(ok)
    setCopyFailed(!ok)
  }

  return (
    <Card>
      <CardTitle>
        <Smartphone className="lucide-inline" aria-hidden="true" />
        {t('pages.settings.mobileLoginCard.sign_in_on_mobile')}
      </CardTitle>
      <p className="mt-2 text-sm leading-relaxed text-muted">
        {t('pages.settings.mobileLoginCard.create_a_one_time_link_for_mobile_or_another_browser')}
      </p>
      {!link ? (
        <Btn className="mt-4" type="button" disabled={createLink.isPending} onClick={() => createLink.mutate()}>
          <Smartphone className="lucide-inline" aria-hidden="true" />
          {createLink.isPending
            ? t('pages.settings.mobileLoginCard.creating_link')
            : t('pages.settings.mobileLoginCard.create_mobile_sign_in_link')}
        </Btn>
      ) : (
        <div className="mt-4">
          <label className="sr-only" htmlFor="mobile-login-link">
            {t('pages.settings.mobileLoginCard.mobile_sign_in_link')}
          </label>
          <Input
            id="mobile-login-link"
            className="w-full font-mono"
            readOnly
            value={link}
            onFocus={event => event.currentTarget.select()}
          />
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <Btn type="button" onClick={() => void copyLink()}>
              <Copy className="lucide-inline" aria-hidden="true" />
              {t('pages.settings.mobileLoginCard.copy_sign_in_link')}
            </Btn>
            <Btn type="button" disabled={createLink.isPending} onClick={() => createLink.mutate()}>
              {createLink.isPending
                ? t('pages.settings.mobileLoginCard.creating_link')
                : t('pages.settings.mobileLoginCard.create_new_mobile_sign_in_link')}
            </Btn>
            {copied && <span className="text-sm text-ok" role="status">{t('pages.settings.mobileLoginCard.link_copied')}</span>}
          </div>
          <p className="mt-3 text-sm leading-relaxed text-muted">
            {t('pages.settings.mobileLoginCard.send_the_copied_link_to_your_mobile_device_then_open_it')}
          </p>
          {expiresIn !== null && (
            <p className="mt-2 text-sm leading-relaxed text-muted">
              {t('pages.settings.mobileLoginCard.link_expires_in_minutes', {
                minutes: Math.ceil(expiresIn / 60),
              })}
            </p>
          )}
        </div>
      )}
      {/* askAgent on: the minted link (if any) is already server-issued and the
          card holds no draft — and `external_origin_unavailable` is exactly the
          config gap (dashboard.url) the agent can fix. */}
      {createLink.isError && (
        <ErrorNotice
          className="mt-3"
          askAgent
          message={t(
            MOBILE_LINK_ERROR_KEYS[
              mobileLinkErrorCode(createLink.error) as keyof typeof MOBILE_LINK_ERROR_KEYS
            ] || 'pages.settings.mobileLoginCard.could_not_create_a_sign_in_link_try_again',
          )}
        />
      )}
      {copyFailed && (
        <ErrorNotice
          className="mt-3"
          askAgent
          message={t('pages.settings.mobileLoginCard.copy_failed_select_the_link_and_copy_it_manually')}
        />
      )}
    </Card>
  )
}
