/**
 * The organization (external IdP) sign-in's form: an optional work e-mail
 * domain or Kiro start URL, and an optional region. KasLoginGate imports it
 * lazily, because the chooser is on the dashboard's first load and this form
 * only renders after the organization choice is clicked. The field values stay
 * in the chooser, so closing and reopening the form keeps what was typed.
 */
import { Btn } from './ui'
import type { KasLoginExtra } from '../hooks/useKasLogin'
import { i18nT } from '../i18n/t'

/** Split the organization lookup field into the hint the portal takes: a value
 *  that reads as a URL is a Kiro start URL, anything else a work e-mail domain
 *  (the gateway also accepts a whole e-mail address and keeps its domain). */
export function organizationLookupExtra(lookup: string, region: string): KasLoginExtra {
  const value = lookup.trim()
  const extra: KasLoginExtra = {}
  const lower = value.toLowerCase()
  if (lower.startsWith('https://') || lower.startsWith('http://')) extra.external_idp_start_url = value
  else if (value) extra.external_idp_domain = value
  if (region.trim()) extra.region = region.trim()
  return extra
}

export default function KasOrganizationForm({
  lookup,
  region,
  busy,
  onLookup,
  onRegion,
  onSubmit,
  onCancel,
}: {
  lookup: string
  region: string
  busy: boolean
  onLookup: (value: string) => void
  onRegion: (value: string) => void
  onSubmit: (extra: KasLoginExtra) => void
  onCancel: () => void
}) {
  return (
    <form
      className="rounded-lg border border-border bg-bg-elevated p-3 sm:col-span-full"
      data-testid="kas-login-org-form"
      aria-labelledby="kas-org-form-title"
      onSubmit={(e) => {
        e.preventDefault()
        if (busy) return
        onSubmit(organizationLookupExtra(lookup, region))
      }}
    >
      <h2 id="kas-org-form-title" className="mb-2 text-[13px] font-semibold text-text-strong">
        {i18nT('components.kasLogin.provider_organization_idp')}
      </h2>
      <p className="mb-2 text-[12px] leading-relaxed text-muted">
        {i18nT('components.kasLogin.organization_idp_hint')}
      </p>
      <label
        className="block text-[12px] font-medium text-text-strong"
        htmlFor="kas-org-lookup"
      >
        {i18nT('components.kasLogin.org_lookup_label')}
        <input
          id="kas-org-lookup"
          type="text"
          aria-label={i18nT('components.kasLogin.org_lookup_label')}
          autoFocus
          value={lookup}
          onChange={(e) => onLookup(e.target.value)}
          className="focus-ring mt-1 w-full rounded-lg border border-border bg-bg px-3 py-2 font-mono text-[13px] font-normal text-text-strong placeholder:text-muted"
        />
      </label>
      <p className="mt-1 text-[12px] leading-relaxed text-muted">
        {i18nT('components.kasLogin.org_helper')}
      </p>
      <p className="mt-1 text-[12px] leading-relaxed text-muted">
        {i18nT('components.kasLogin.org_browser_note')}
      </p>
      <label
        className="mt-3 block text-[12px] font-medium text-text-strong"
        htmlFor="kas-org-region"
      >
        {i18nT('components.kasLogin.sso_region_label')}
        <input
          id="kas-org-region"
          type="text"
          aria-label={i18nT('components.kasLogin.sso_region_label')}
          value={region}
          onChange={(e) => onRegion(e.target.value)}
          placeholder="us-east-1"
          className="focus-ring mt-1 w-full rounded-lg border border-border bg-bg px-3 py-2 font-mono text-[13px] font-normal text-text-strong placeholder:text-muted"
        />
      </label>
      <p className="mt-1 text-[12px] leading-relaxed text-muted">
        {i18nT('components.kasLogin.org_region_hint')}
      </p>
      <div className="mt-3 flex items-center gap-2">
        <Btn type="submit" primary disabled={busy}>
          {i18nT('components.kasLogin.sso_continue')}
        </Btn>
        <Btn type="button" disabled={busy} onClick={onCancel}>
          {i18nT('components.kasLogin.sso_cancel')}
        </Btn>
      </div>
    </form>
  )
}
