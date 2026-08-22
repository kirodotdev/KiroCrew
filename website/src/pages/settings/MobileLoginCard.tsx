import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Copy, Smartphone } from 'lucide-react'
import { api } from '../../api/client'
import { Btn, Card, CardTitle, Input } from '../../components/ui'
import { copyToClipboard } from '../../utils/clipboard'

export function MobileLoginCard() {
  const { t } = useTranslation()
  const [link, setLink] = useState('')
  const [copied, setCopied] = useState(false)
  const [copyFailed, setCopyFailed] = useState(false)

  const createLink = useMutation({
    mutationFn: api.mobileLoginLink,
    onMutate: () => {
      setCopied(false)
      setCopyFailed(false)
    },
    onSuccess: result => {
      setLink(result.url)
    },
  })

  const copyLink = async () => {
    if (!link) return
    try {
      await copyToClipboard(link)
      setCopied(true)
      setCopyFailed(false)
    } catch {
      setCopied(false)
      setCopyFailed(true)
    }
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
            {copied && <span className="text-sm text-ok" role="status">{t('pages.settings.mobileLoginCard.link_copied')}</span>}
          </div>
          <p className="mt-3 text-sm leading-relaxed text-muted">
            {t('pages.settings.mobileLoginCard.send_the_copied_link_to_your_mobile_device_then_open_it')}
          </p>
        </div>
      )}
      {createLink.isError && (
        <p className="mt-3 text-sm text-danger" role="alert">
          {t('pages.settings.mobileLoginCard.could_not_create_a_sign_in_link_try_again')}
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
