import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Bot, Sparkles, Terminal } from 'lucide-react'

import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsCard, SettingsButtonGroup } from '../../components/settings'
import { useConfigSchema } from '../../components/settingRef/useConfigSchema'
import { i18nT } from '../../i18n/t'

/** The config field the switch owns. Also the schema path the options are gated on. */
const CONFIG_KEY = 'agent.acp_backend'

/**
 * Backend ids, verbatim from `acp/types.py`. `''` (Kiro CLI) is the shipped
 * default and is a REAL value, not "unset" — the empty string is how the core
 * spells the Kiro backend, so it must round-trip as itself.
 */
const KIRO = ''
const CLAUDE = 'claude'
const KAS = 'kas'
const OPENCODE = 'opencode'

/**
 * DOM id of the row that states a backend's status.
 *
 * The option button carries this as `aria-describedby`, so the reason a choice is
 * dead reaches a screen reader instead of living in visual proximity only. KIRO is
 * the empty string, hence the explicit `kiro` fallback — an id must not end in the
 * bare separator.
 */
const statusId = (value: string) => `agent-backend-status-${value || 'kiro'}`

/**
 * Developer > Agent Backend — pick which agent runs a session.
 *
 * ## Why this exists again
 *
 * The public core used to ship a multi-provider `ProviderPanel` and deleted it
 * when it collapsed to Kiro CLI only (`refactor(website): collapse provider layer
 * to KiroACP-only`). The backend kept all three agents wired the whole time, so
 * `agent.acp_backend` has been switchable with no way to switch it. This is that
 * control, minus the dead parts of the old panel (Bedrock model ids, a Claude Code
 * migration wizard, a provider enum that now has exactly one member).
 *
 * ## Why the choices come from the server
 *
 * Every agent the code knows about is listed, but only the ones this build can
 * actually run are selectable — that set is read from `GET /api/config/schema`
 * (`enumValues`), which the backend resolves per request from
 * `acp_backends.selectable_backend_values()`, the same owner
 * `PATCH /api/config/kirocrew` validates against. So the enabled options and the
 * values the wire accepts cannot disagree, and a build that ships another agent
 * lights it up here with no frontend change.
 *
 * Claude Code is the case that makes this worth doing: this build does not include
 * it. Hiding it would imply it does not exist; enabling it would produce a 400 from
 * a control that looked live. It is listed, disabled, and says which it is.
 *
 * ## Why each row says so little
 *
 * An earlier revision wrote a prose sentence per agent claiming what each one
 * supports — sandboxing, shared processes, mid-turn steer, subagent progress.
 * Those claims were not measured anywhere; they were asserted here, in the view
 * layer, where nothing can contradict them. They were wrong in the ways
 * unmeasured claims usually are.
 *
 * The status line per row is therefore limited to what this build can actually
 * establish, and the vocabulary is taken from the ACP-adapter card rather than
 * invented again: `Default. All features supported.` for the backend whose
 * descriptor is all-supported, `Experimental` for one that is not, and a
 * not-enabled line for one this build cannot run. Per-capability detail
 * (which feature is supported, degraded, or unverified per backend) needs the
 * descriptor table that owns those facts and is deliberately NOT restated here.
 *
 * Deliberately NOT under `pages/settings/`: `gen-settings-registry.mjs` scans that
 * directory, and indexing an agent switch into Settings search would advertise it
 * as an ordinary preference — it changes which agent binary runs.
 */
export function AgentBackendTab() {
  const qc = useQueryClient()
  const [saveError, setSaveError] = useState('')
  const schema = useConfigSchema()

  const cfgQ = useQuery<{ agent?: { acp_backend?: string } }>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })

  const patchMut = useMutation({
    mutationFn: (value: string) => api.patchConfig(CONFIG_KEY, value),
    onSuccess: () => {
      setSaveError('')
      qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    },
    // No optimistic write and no local mirror of the value: the button group reads
    // straight from the query, so a rejected PATCH needs no revert — the cache was
    // never moved off the server's answer.
    onError: () => setSaveError(i18nT('pages.developer.agentBackendTab.could_not_save_the_agent_backend')),
  })

  if (cfgQ.isLoading) {
    return (
      <div className="text-muted text-sm py-12 text-center">
        {i18nT('pages.developer.agentBackendTab.loading_configuration')}
      </div>
    )
  }

  /**
   * A failed read is NOT the default value.
   *
   * `?? KIRO` is right for a config that genuinely omits the key — the shipped
   * default really is Kiro CLI. It is wrong for a read that FAILED: the value is
   * then unknown, and defaulting paints Kiro CLI as the pressed option, so an
   * operator running KAS is shown the wrong agent by a control that looks live.
   * Offer the retry instead of guessing.
   */
  if (cfgQ.isError) {
    return (
      <div className="py-12 text-center">
        <div className="text-muted text-sm">
          {i18nT('pages.developer.agentBackendTab.could_not_load_the_agent_backend')}
        </div>
        <button
          type="button"
          className="mt-3 text-[13px] px-3 py-[5px] rounded-md border border-border bg-bg-elevated text-text-strong cursor-pointer"
          onClick={() => cfgQ.refetch()}
        >
          {i18nT('pages.developer.agentBackendTab.retry')}
        </button>
      </div>
    )
  }

  const current = cfgQ.data?.agent?.acp_backend ?? KIRO

  /**
   * `undefined` while the schema is in flight — every option stays enabled rather
   * than flashing disabled and then live, which would read as a broken control on
   * a slow load. The PATCH allowlist is the real gate either way, so an optimistic
   * enable can only cost one visible refusal.
   */
  const selectable = schema?.get(CONFIG_KEY)?.enum
  const unavailable = (value: string) => (selectable ? !selectable.includes(value) : false)

  const NAME: Record<string, string> = {
    [KIRO]: i18nT('pages.developer.agentBackendTab.kiro_cli'),
    [CLAUDE]: i18nT('pages.developer.agentBackendTab.claude_code'),
    [KAS]: i18nT('pages.developer.agentBackendTab.kas_kiro_agent'),
    [OPENCODE]: i18nT('pages.developer.agentBackendTab.opencode'),
  }

  /**
   * The one status line a row carries, derived rather than authored per agent.
   *
   * Not-enabled is checked FIRST and off the schema, so a build that starts
   * shipping an agent stops calling it unavailable without an edit here. KIRO is
   * the all-supported descriptor, so it gets that sentence; anything else that is
   * selectable is not, so it gets `Experimental` rather than a claim.
   */
  const status = (value: string): string => {
    if (unavailable(value)) return i18nT('pages.developer.agentBackendTab.not_enabled_in_this_build')
    if (value === KIRO) return i18nT('pages.developer.agentBackendTab.default_all_features_supported')
    return i18nT('pages.developer.agentBackendTab.experimental')
  }

  return (
    <>
      <ErrorNotice message={saveError} onDismiss={() => setSaveError('')} />
      <SettingsCard>
        <SettingsButtonGroup
          label={i18nT('pages.developer.agentBackendTab.agent_backend')}
          description={i18nT('pages.developer.agentBackendTab.new_sessions_use_this_agent_a_session_that_is_al')}
          configKey={CONFIG_KEY}
          value={current}
          disabled={patchMut.isPending}
          options={[
            {
              value: KIRO,
              label: NAME[KIRO],
              icon: <Terminal size={14} />,
              disabled: unavailable(KIRO),
              describedById: statusId(KIRO),
            },
            {
              value: CLAUDE,
              label: NAME[CLAUDE],
              icon: <Sparkles size={14} />,
              disabled: unavailable(CLAUDE),
              describedById: statusId(CLAUDE),
            },
            {
              value: KAS,
              label: NAME[KAS],
              icon: <Bot size={14} />,
              disabled: unavailable(KAS),
              describedById: statusId(KAS),
            },
            {
              value: OPENCODE,
              label: NAME[OPENCODE],
              icon: <Terminal size={14} />,
              disabled: unavailable(OPENCODE),
              describedById: statusId(OPENCODE),
            },
          ]}
          onChange={v => patchMut.mutate(v)}
        />
        {/* One line per agent, always all four — the reader is choosing BETWEEN
            them, so showing only the selected one's status would hide the very
            comparison the control is for. */}
        <dl className="mt-2 space-y-1.5">
          {[KIRO, CLAUDE, KAS, OPENCODE].map(value => (
            <div key={value} className="flex gap-2 text-[11px] leading-relaxed">
              <dt className={`shrink-0 font-semibold ${value === current ? 'text-text-strong' : 'text-muted'}`}>
                {NAME[value]}
              </dt>
              <dd
                id={statusId(value)}
                className={`m-0 ${unavailable(value) ? 'text-warn' : 'text-muted'}`}
              >
                {status(value)}
              </dd>
            </div>
          ))}
        </dl>
      </SettingsCard>
    </>
  )
}
