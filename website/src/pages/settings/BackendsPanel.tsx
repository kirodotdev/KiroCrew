import { useQuery } from '@tanstack/react-query'

import { api } from '../../api/client'
import { SettingsSection, SettingsCard } from '../../components/settings'
import { Badge } from '../../components/ui'
import { i18nT } from '../../i18n/t'

/** Query key for the panel's full listing. Distinct from the composer's
 *  `['backends']` rows so the two readers cannot hand each other a shape: this
 *  surface consumes the invalid/unroutable diagnostics too, and a shared key
 *  would let one shape's cache decide the other's content. */
const REGISTRY_KEY = ['backendsRegistry'] as const

/**
 * AI Backends — the inventory the descriptor-harness registry made of what this
 * build offers and what an operator wrote.
 *
 * Read-only, by design (D3/D5). Three groups:
 *
 * - **Selectable** backends — every id a new chat may run on (id + display
 *   label), with the global default marked. This is the same set the composer's
 *   picker offers, from the single selectability owner.
 * - **Unroutable** operator descriptors — spellable, but they declared no
 *   verified routing, so the gateway refused them selectability. Shown with the
 *   reason so the operator sees WHY their entry is not offered.
 * - **Invalid** operator descriptors — failed validation, so they are not even
 *   spellable. Shown with every reason they were rejected.
 *
 * Backend DEFINITIONS are deliberately not editable here: a descriptor names a
 * binary Kiro Crew will spawn, so it is config-file-only (`harnesses.json`,
 * write-protected — see D5) and off the Settings PATCH allowlist. Editing it
 * needs a gateway restart to re-run the boot-load. This panel reads the registry
 * and reports what it made of those definitions.
 *
 * The GLOBAL default selector is NOT duplicated here — it lives in its own
 * surface (the developer AgentBackendTab) and stays there; this panel only
 * REPORTS which backend is currently the default.
 */
export function BackendsPanel() {
  const regQ = useQuery({
    queryKey: REGISTRY_KEY,
    queryFn: () => api.backends(),
  })
  const selectable = regQ.data?.backends ?? []
  const unroutable = regQ.data?.unroutable ?? []
  const invalid = regQ.data?.invalid ?? []
  const total = selectable.length + unroutable.length + invalid.length

  return (
    <>
      <SettingsSection title={i18nT('pages.settings.backendsPanel.ai_backends')}>
        <SettingsCard>
          <div
            className="text-[12px] text-muted"
            data-setting-label={i18nT('pages.settings.backendsPanel.ai_backends')}
          >
            {i18nT('pages.settings.backendsPanel.backends_are_defined_in_config_and_need_a_restart')}
          </div>
          {regQ.isError ? (
            // Selectability + the diagnostics are the whole content of every row,
            // so a listing that did not answer renders nothing rather than a
            // stale verdict.
            <div className="text-[13px] text-muted py-2">
              {i18nT('pages.settings.backendsPanel.backend_list_unavailable')}
            </div>
          ) : !regQ.isSuccess ? null : total === 0 ? (
            // Only after a successful listing: "none registered" is a claim, and
            // stating it while the fetch is still in flight would assert it of
            // every install for the first frame.
            <div className="text-[13px] text-muted py-2">
              {i18nT('pages.settings.backendsPanel.no_backends_registered')}
            </div>
          ) : (
            <ul className="list-none p-0 m-0 flex flex-col gap-2" data-testid="backend-inventory">
              {selectable.map(row => (
                <li
                  key={row.id || '__kiro__'}
                  className="flex flex-col gap-1 py-1.5 border-b border-border last:border-b-0"
                  data-testid={`backend-row-${row.id || 'kiro'}`}
                >
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-[13px] font-semibold text-text-strong">{row.label || row.id}</span>
                    {row.id && <span className="text-[12px] text-muted font-mono">{row.id}</span>}
                    <Badge variant="ok">
                      {i18nT('pages.settings.backendsPanel.badge_selectable')}
                    </Badge>
                    {row.is_global_default && (
                      <Badge variant="aim" data-testid={`backend-default-${row.id || 'kiro'}`}>
                        {i18nT('pages.settings.backendsPanel.badge_default')}
                      </Badge>
                    )}
                  </div>
                </li>
              ))}
              {unroutable.map(row => (
                <li
                  key={`unroutable-${row.id}`}
                  className="flex flex-col gap-1 py-1.5 border-b border-border last:border-b-0"
                  data-testid={`backend-unroutable-${row.id}`}
                >
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-[13px] font-semibold text-text-strong">{row.label || row.id}</span>
                    <span className="text-[12px] text-muted font-mono">{row.id}</span>
                    <Badge variant="warn">
                      {i18nT('pages.settings.backendsPanel.badge_unroutable')}
                    </Badge>
                  </div>
                  <div className="text-[12px] text-warn">{row.reason}</div>
                </li>
              ))}
              {invalid.map(row => (
                <li
                  key={`invalid-${row.id}`}
                  className="flex flex-col gap-1 py-1.5 border-b border-border last:border-b-0"
                  data-testid={`backend-invalid-${row.id}`}
                >
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-[13px] font-semibold text-text-strong">{row.label || row.id}</span>
                    <span className="text-[12px] text-muted font-mono">{row.id}</span>
                    <Badge variant="err">
                      {i18nT('pages.settings.backendsPanel.badge_invalid')}
                    </Badge>
                  </div>
                  {row.reasons.map((reason, i) => (
                    <div key={i} className="text-[12px] text-danger">{reason}</div>
                  ))}
                </li>
              ))}
            </ul>
          )}
        </SettingsCard>
      </SettingsSection>
    </>
  )
}
