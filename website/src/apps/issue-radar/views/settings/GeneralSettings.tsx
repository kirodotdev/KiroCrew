import { useEffect, useRef } from 'react'
import { Plus, ChevronRight, Settings as SettingsIcon } from 'lucide-react'
import { ProviderLogo } from '../../components/ProviderBadge'
import { providerTerms } from '../../lib/links'
import { useIssueRadar } from '../../context'
import ReadOnlyTag, { isReadOnly } from '../../components/ReadOnlyTag'
import type { GeneralAnchor } from '../../lib/types'

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
      <h1 className="text-[22px] font-semibold mb-1">Settings</h1>
      <p className="text-[13px] text-muted mb-8">
        Your {terms.providerName} identity and the repositories Issue Radar watches.
      </p>

      <section ref={accountRef} className="mb-10 scroll-mt-8">
        <SectionHeader title="Account" />
        <div className="rounded-xl border border-border bg-bg-elevated shadow-sm p-5">
          {me ? (
            <div className="flex items-center gap-3">
              <div className="w-9 h-9 rounded-full bg-bg-hover flex items-center justify-center flex-shrink-0">
                <ProviderLogo repoRef={active} size={18} />
              </div>
              <div className="min-w-0">
                <div className="text-[14px] font-medium">{me}</div>
                <div className="text-[12px] text-muted">
                  Authenticated through your local <code>{terms.cli}</code> CLI — Issue Radar
                  keeps no credentials.
                </div>
              </div>
            </div>
          ) : (
            <div className="text-[13px] text-muted">
              Not signed in — the <code>{terms.cli}</code> CLI has no active session. Run{' '}
              <code>{terms.cli} auth login</code> in your terminal.
            </div>
          )}
        </div>
      </section>

      <section ref={reposRef} className="scroll-mt-8">
        <SectionHeader title="Repositories" hint={`${repos.length} connected`} />
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
                  <SettingsIcon size={11} /> Configure triage
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
            <span className="text-[14px]">Connect another repo</span>
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
