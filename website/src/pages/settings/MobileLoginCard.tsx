import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Copy, Smartphone } from 'lucide-react'
import { api } from '../../api/client'
import { Btn, Card, CardTitle, Input } from '../../components/ui'
import { useAppSelector } from '../../store'
import { parseErrorCode } from '../../utils/errorReport'
import { copyToClipboard } from '../../utils/clipboard'

const mobileLinkErrorCode = (error: unknown): string | undefined =>
  typeof error === 'object' && error !== null && 'body' in error
    ? parseErrorCode(typeof error.body === 'string' ? error.body : undefined)
    : undefined

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
      {createLink.isError && (
        <p className="mt-3 text-sm text-danger" role="alert">
          {mobileLinkErrorCode(createLink.error) === 'external_origin_unavailable'
            ? t('pages.settings.mobileLoginCard.dashboard_url_required_to_create_a_mobile_sign_in_link')
            : t('pages.settings.mobileLoginCard.could_not_create_a_sign_in_link_try_again')}
        </p>
      )}
      {copyFailed && (
        <p className="mt-3 text-sm text-danger" role="alert">
          {t('pages.settings.mobileLoginCard.copy_failed_select_the_link_and_copy_it_manually')}
        </p>
      )}
    </Card>
  )
}
