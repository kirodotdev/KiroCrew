/**
 * Settings -> Browser -> More -> the `browser.credentials.domains` editor: the
 * sites the edition's sign-in may be used for in Kiro's Chromium. Shown only
 * when the edition supplies a sign-in provider.
 *
 * Chips with a remove button plus an add field, written as ONE value (the whole
 * array), the shape the PATCH route stores. An entry is refused here with the
 * gateway's own rule (`normalizeCredentialDomain`); an empty list follows the
 * provider's own default.
 */
import { Plus, X } from 'lucide-react'
import { useId, useState } from 'react'

import { SettingsField } from '../../components/settings'
import { Btn, Input } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { normalizeCredentialDomain } from './browserStatus'

/** Mirrors the gateway's `CREDENTIAL_DOMAINS_CAP`. */
export const CREDENTIAL_DOMAINS_MAX = 64

export function BrowserCredentialDomains({ domains, provider, disabled, onChange }: {
  domains: string[]
  provider: string
  disabled: boolean
  onChange: (next: string[]) => void
}) {
  const [draft, setDraft] = useState('')
  const [invalid, setInvalid] = useState(false)
  const inputId = useId()
  const invalidId = useId()
  const full = domains.length >= CREDENTIAL_DOMAINS_MAX

  const add = () => {
    const domain = normalizeCredentialDomain(draft)
    if (!domain) { setInvalid(true); return }
    setInvalid(false)
    setDraft('')
    if (domains.includes(domain)) return
    onChange([...domains, domain])
  }
  const remove = (domain: string) => onChange(domains.filter(d => d !== domain))

  return (
    <div>
      <SettingsField
        label={i18nT('pages.settings.browserPanel.cred_domains_label')}
        hint={i18nT('pages.settings.browserPanel.cred_domains_hint', { provider })}
        configKey="browser.credentials.domains"
        controlId={inputId}
      >
        <ul className="list-none m-0 p-0 flex flex-wrap items-center gap-1.5" aria-label={i18nT('pages.settings.browserPanel.cred_domains_label')}>
          {domains.map(d => (
            <li key={d} className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-elevated pl-2 pr-1 py-0.5 font-mono text-[12px] text-text">
              <span>{d}</span>
              <Btn
                onClick={() => remove(d)}
                disabled={disabled}
                aria-label={i18nT('pages.settings.browserPanel.remove_cred_domain', { domain: d })}
                className="!px-1 !py-0.5"
              >
                <X size={12} className="lucide-inline" aria-hidden />
              </Btn>
            </li>
          ))}
          {domains.length === 0 && (
            <li className="text-[12px] text-muted">
              {i18nT('pages.settings.browserPanel.cred_domains_empty', { provider })}
            </li>
          )}
        </ul>
        <form
          className="flex items-center gap-2 pt-1"
          onSubmit={e => { e.preventDefault(); add() }}
        >
          <Input
            id={inputId}
            value={draft}
            onChange={e => { setDraft(e.target.value); if (invalid) setInvalid(false) }}
            placeholder="example.com"
            disabled={disabled || full}
            spellCheck={false}
            autoCorrect="off"
            autoCapitalize="off"
            aria-invalid={invalid || undefined}
            aria-describedby={invalid ? invalidId : undefined}
            className="font-mono text-[12px] max-w-[260px]"
          />
          <Btn type="submit" disabled={disabled || full || !draft.trim()} data-testid="browser-cred-domain-add">
            <Plus size={13} className="lucide-inline" aria-hidden />
            {i18nT('pages.settings.browserPanel.add_cred_domain')}
          </Btn>
        </form>
        {/* A validation hint, not an error: nothing was sent. Plain text,
            no alert role. */}
        {invalid && (
          <p id={invalidId} className="text-[12px] text-warn-text m-0" data-testid="browser-cred-domain-invalid">
            {i18nT('pages.settings.browserPanel.cred_domain_invalid')}
          </p>
        )}
        {full && (
          <p className="text-[12px] text-muted m-0">
            {i18nT('pages.settings.browserPanel.cred_domains_full', { count: CREDENTIAL_DOMAINS_MAX })}
          </p>
        )}
      </SettingsField>
    </div>
  )
}
