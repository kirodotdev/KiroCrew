import { ExternalLink } from 'lucide-react'
import { isKiroHarness, type AcpBackendConfig } from '../../api/acpBackend'
import { i18nT } from '../../i18n/t'

export const KIRO_MODEL_SETTINGS_URL = 'https://app.kiro.dev/settings/model'

/**
 * Note above the fallback settings: on the Kiro CLI and kiro-agent harnesses the
 * fallbacks managed on app.kiro.dev run inside the harness first, and the
 * settings on this page apply on top. Renders nothing on other harnesses, where
 * Kiro Crew's own fallback is the only one.
 */
export function KiroHarnessFallbackNote({ cfg }: { cfg: AcpBackendConfig | undefined }) {
  if (!isKiroHarness(cfg)) return null
  return (
    <div className="mb-4 rounded-md border border-border bg-bg-elevated px-3 py-2 flex flex-col gap-1" data-testid="kiro-harness-fallback-note">
      <span className="text-[12px] font-medium text-text">{i18nT('pages.settings.chatPanel.kiro_harness_fallback_title')}</span>
      <span className="text-[12px] text-muted leading-relaxed">{i18nT('pages.settings.chatPanel.kiro_harness_fallback_body')}</span>
      <a
        href={KIRO_MODEL_SETTINGS_URL}
        target="_blank" rel="noopener noreferrer"
        className="text-[12px] text-accent hover:underline w-fit inline-flex items-center gap-1"
      >
        {i18nT('pages.settings.chatPanel.kiro_harness_fallback_link')}
        <ExternalLink size={12} aria-hidden />
      </a>
    </div>
  )
}
