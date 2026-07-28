import { useEffect, useRef } from 'react'
import { Plus, ChevronRight, Settings as SettingsIcon } from 'lucide-react'
import { ProviderLogo } from '../../components/ProviderBadge'
import { providerTerms } from '../../lib/links'
import { useIssueRadar } from '../../context'
import ReadOnlyTag, { isReadOnly } from '../../components/ReadOnlyTag'
import type { GeneralAnchor } from '../../lib/types'

import { i18nT } from '../../../../i18n/t'
/** General (app-wide) settings — full width. The GitHub identity and the list
 * of connected repos. Each repo card jumps to that repo's own settings page.
 * `anchor` scrolls to the requested sub-section when the rail asks for it. */
export default function GeneralSettings({ anchor }: { anchor: GeneralAnchor }) {
  const { me, repos, onAddRepo, openSettings, active } = useIssueRadar()
  // The account shown is the one on the ACTIVE repo's provider — `me` is fetched
  // per provider, so naming the wrong CLI here would contradict the login above it.
  const terms = providerTerms(active)
  const accountRef = useRef<HTMLElement>(null)
  const reposRef = useRef<HTMLElement>(null)

  useEffect(() => {
    const ref = anchor === 'repos' ? reposRef : accountRef
    ref.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [anchor])

  return (
    <div className="w-full max-w-6xl px-8 py-8">
      <h1 className="text-[22px] font-semibold mb-1">{i18nT('apps.issueRadar.views.settings.generalSettings.settings')}</h1>
      <p className="text-[13px] text-muted mb-8">
        {i18nT('apps.issueRadar.views.settings.generalSettings.your')} {terms.providerName} {i18nT('apps.issueRadar.views.settings.generalSettings.identity_and_the_repositories_issue_radar_watche')}
      </p>

      <section ref={accountRef} className="mb-10 scroll-mt-8">
        <SectionHeader title={i18nT('apps.issueRadar.views.settings.generalSettings.account')} />
        <div className="rounded-xl border border-border bg-bg-elevated shadow-sm p-5">
          {me ? (
            <div className="flex items-center gap-3">
              <div className="w-9 h-9 rounded-full bg-bg-hover flex items-center justify-center flex-shrink-0">
                <ProviderLogo repoRef={active} size={18} />
              </div>
              <div className="min-w-0">
                <div className="text-[14px] font-medium">{me}</div>
                <div className="text-[12px] text-muted">
                  {i18nT('apps.issueRadar.views.settings.generalSettings.authenticated_through_your_local')} <code>{terms.cli}</code> {i18nT('apps.issueRadar.views.settings.generalSettings.cli_issue_radar_keeps_no_credentials')}
                </div>
              </div>
            </div>
          ) : (
            <div className="text-[13px] text-muted">
              {i18nT('apps.issueRadar.views.settings.generalSettings.not_signed_in_the')} <code>{terms.cli}</code> {i18nT('apps.issueRadar.views.settings.generalSettings.cli_has_no_active_session_run')}{' '}
              <code>{terms.cli} {i18nT('apps.issueRadar.views.settings.generalSettings.auth_login')}</code> {i18nT('apps.issueRadar.views.settings.generalSettings.in_your_terminal')}
            </div>
          )}
        </div>
      </section>

      <section ref={reposRef} className="scroll-mt-8">
        <SectionHeader title={i18nT('apps.issueRadar.views.settings.generalSettings.repositories')} hint={`${repos.length} connected`} />
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {repos.map((r) => (
            <button
              key={`${r.owner}/${r.repo}`}
              onClick={() => openSettings({ kind: 'repo', owner: r.owner, repo: r.repo, provider: r.provider, host: r.host })}
              className="group text-left rounded-xl border border-border bg-bg-elevated shadow-sm p-4 hover:border-border-strong hover:bg-bg-hover cursor-pointer transition-colors flex items-center gap-3"
            >
              <ProviderLogo repoRef={r} size={16} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-[14px] font-medium truncate">{r.owner}/{r.repo}</span>
                  {isReadOnly(r.permissions) && <ReadOnlyTag />}
                </div>
                <div className="text-[12px] text-muted mt-0.5 inline-flex items-center gap-1">
                  <SettingsIcon size={11} /> {i18nT('apps.issueRadar.views.settings.generalSettings.configure_triage')}
                </div>
              </div>
              <ChevronRight size={16} className="text-muted flex-shrink-0 group-hover:text-text transition-colors" />
            </button>
          ))}

          <button
            onClick={onAddRepo}
            className="text-left rounded-xl border border-dashed border-border p-4 text-muted hover:text-text hover:border-border-strong hover:bg-bg-hover cursor-pointer transition-colors flex items-center gap-2 bg-transparent"
          >
            <Plus size={16} className="flex-shrink-0" />
            <span className="text-[14px]">{i18nT('apps.issueRadar.views.settings.generalSettings.connect_another_repo')}</span>
          </button>
        </div>
      </section>
    </div>
  )
}

function SectionHeader({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex items-baseline justify-between mb-3">
      <h2 className="text-[13px] font-semibold text-muted uppercase tracking-[.06em]">{title}</h2>
      {hint && <span className="text-[12px] text-muted opacity-70">{hint}</span>}
    </div>
  )
}
