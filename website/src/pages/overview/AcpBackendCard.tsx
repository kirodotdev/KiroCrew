/**
 * ACP backend selector — the Settings control for `agent.acp_backend`.
 *
 * Renders one row per SELECTABLE backend, read from `GET /api/acp-backends`.
 * The capability table is never duplicated here: the gateway owns it, so this
 * card and `kirocrew doctor` cannot disagree about what a backend supports.
 *
 * A known-but-unselectable backend is deliberately NOT rendered as a disabled
 * row. Showing a control that can never be operated invites the reader to hunt
 * for the setting that would enable it; a footnote naming those backends and
 * pointing at the doc is honest about where the decision actually lives.
 */
import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useLocation } from 'react-router-dom'
import { FlaskConical, ChevronDown, ChevronRight, AlertTriangle, Cpu, PackageX } from 'lucide-react'

import { api } from '../../api/client'
import { Btn, Card, CardTitle, ContentSkeleton } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import Modal from '../../components/Modal'
import { i18nT } from '../../i18n/t'

/** DOM id this card carries, and the route that scrolls to it.
 *
 *  The card is deep-linked from the top-bar harness readout, so the anchor and
 *  the route are exported from the card itself rather than spelled at the call
 *  site: moving the card moves both. `?tab=config` is `SidePanelLayout`'s tab
 *  param on Developer.
 *
 *  Deliberately NOT the `useSettingHighlight` `highlight=key:` mechanism: that
 *  hook resolves its target SYNCHRONOUSLY on its first effect, while the
 *  backend rows arrive after `GET /api/acp-backends` completes — a registry
 *  refresh, so hundreds of ms at best. The scroll therefore lives here, where
 *  completion is observable. */
export const ACP_BACKEND_ANCHOR = 'acp-adapter'
export const ACP_BACKEND_ROUTE = `/developer?tab=acp-adapters#${ACP_BACKEND_ANCHOR}`
const ACP_BACKEND_DOCS =
  'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/experimental-acp-adapters.md'

/** One backend as the gateway describes it. Mirrors `_descriptor_payload`. */
export interface AcpBackendRow {
  id: string
  label: string
  experimental: boolean
  selectable: boolean
  signin_command: string
  install_command: string
  /** '' when not probed, else 'installed' | 'missing' | 'unknown'. */
  installed: string
  dialect: string
  routing: string
  capabilities: Record<string, string>
  degraded_count: number
}

export interface AcpBackendsPayload {
  active: string
  allow_ungated_tools: boolean
  routing_verdict: string
  routing_reason?: string
  backends: AcpBackendRow[]
}

/** Capability level → the i18n key naming it, and the token that colours it.
 *
 *  Four states, never collapsed to a boolean: "works differently",
 *  "not verified", and "is missing" are separate facts an operator needs to tell apart. */
const LEVEL_KEYS = {
  supported: 'pages.overview.acpBackend.level_supported',
  degraded: 'pages.overview.acpBackend.level_degraded',
  unavailable: 'pages.overview.acpBackend.level_unavailable',
  unverified: 'pages.overview.acpBackend.level_unverified',
} as const

const LEVEL_CLASS: Record<string, string> = {
  supported: 'text-ok',
  degraded: 'text-warn',
  unavailable: 'text-danger',
  unverified: 'text-warn',
}

/** Capability id → its i18n key. An `as const` map rather than a computed key,
 *  so `check-i18n-keys.mjs` can statically verify every one of them. */
const CAPABILITY_KEYS = {
  session_sharing: 'pages.overview.acpBackend.cap_session_sharing',
  reasoning_effort: 'pages.overview.acpBackend.cap_reasoning_effort',
  mcp_tool_search: 'pages.overview.acpBackend.cap_mcp_tool_search',
  agent_profiles: 'pages.overview.acpBackend.cap_agent_profiles',
  slash_commands: 'pages.overview.acpBackend.cap_slash_commands',
  turn_usage: 'pages.overview.acpBackend.cap_turn_usage',
  billing: 'pages.overview.acpBackend.cap_billing',
  native_resume: 'pages.overview.acpBackend.cap_native_resume',
  registry_model_ids: 'pages.overview.acpBackend.cap_registry_model_ids',
  mid_turn_steer: 'pages.overview.acpBackend.cap_mid_turn_steer',
} as const

/** Tool-gate verdict → its i18n key. An `as const` map rather than a computed
 *  key, so `check-i18n-keys.mjs` can statically verify every one of them. */
const ROUTING_KEYS = {
  routed: 'pages.overview.acpBackend.routing_routed',
  indeterminate: 'pages.overview.acpBackend.routing_indeterminate',
  bypassed: 'pages.overview.acpBackend.routing_bypassed',
} as const

function capabilityLabel(id: string): string {
  const key = CAPABILITY_KEYS[id as keyof typeof CAPABILITY_KEYS]
  // An unknown capability is a gateway newer than this bundle. Show its raw id
  // rather than dropping the row: a missing line would understate the change.
  return key ? i18nT(key) : id
}

function levelLabel(level: string): string {
  const key = LEVEL_KEYS[level as keyof typeof LEVEL_KEYS]
  return key ? i18nT(key) : level
}

interface Props {
  /** Persist `agent.acp_backend`. Provided by the settings page that owns save. */
  onSave: (path: string, value: string) => Promise<unknown>
}

export function AcpBackendCard({ onSave }: Props) {
  const { data, error, isError, refetch } = useQuery<AcpBackendsPayload>({
    queryKey: ['acp-backends', { probe: true }],
    queryFn: () => api.acpBackends({ probe: true }) as Promise<AcpBackendsPayload>,
  })
  const [expanded, setExpanded] = useState<string | null>(null)
  const [pending, setPending] = useState<AcpBackendRow | null>(null)
  const [saving, setSaving] = useState(false)

  // Deep-link arrival: bring the card into view once it actually exists. Gated
  // on `data` because the card renders nothing before the fetch resolves, and
  // on the hash because the Config tab is also opened directly — an unrequested
  // scroll past the rows above would lose the reader their place.
  const { hash } = useLocation()
  useEffect(() => {
    if (!data || hash !== `#${ACP_BACKEND_ANCHOR}`) return
    document
      .getElementById(ACP_BACKEND_ANCHOR)
      ?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [data, hash])

  if (!data) {
    // Owner-only endpoint: a non-owner viewer gets 403. Render nothing rather
    // than an error — the control is not theirs to operate. Every other failure
    // stays visible because this card owns the dedicated tab's entire body.
    if (isError && (error as { status?: unknown } | null)?.status === 403) return null
    return (
      <Card id={ACP_BACKEND_ANCHOR}>
        <CardTitle>
          <Cpu className="lucide-inline" /> {i18nT('pages.overview.acpBackend.title')}
        </CardTitle>
        {isError ? (
          <div className="flex flex-col items-start gap-2">
            <ErrorNotice
              message={i18nT('pages.overview.acpBackend.load_failed')}
              askAgent
              className="w-full"
            />
            <Btn type="button" onClick={() => void refetch()}>
              {i18nT('components.kiroPrerequisiteGate.try_again')}
            </Btn>
          </div>
        ) : (
          <ContentSkeleton rows={4} />
        )}
      </Card>
    )
  }

  const selectable = data.backends.filter((b) => b.selectable)
  const withheld = data.backends.filter((b) => !b.selectable && b.experimental)

  const choose = (row: AcpBackendRow) => {
    if (row.id === data.active) return
    // Every backend switch clears model selections because model ids belong to
    // the harness namespace. The confirmation is therefore required in both
    // directions; returning to Kiro is operationally safe but not lossless.
    setPending(row)
  }

  const commit = async (row: AcpBackendRow) => {
    setSaving(true)
    try {
      await onSave('agent.acp_backend', row.id)
      await refetch()
    } finally {
      setSaving(false)
      setPending(null)
    }
  }

  return (
    <Card id={ACP_BACKEND_ANCHOR}>
      <CardTitle>
        <Cpu className="lucide-inline" /> {i18nT('pages.overview.acpBackend.title')}
      </CardTitle>

      {data.routing_verdict && (
        <p className={data.routing_verdict === 'routed' ? 'text-ok' : 'text-warn'} role="status">
          {i18nT(ROUTING_KEYS[data.routing_verdict as keyof typeof ROUTING_KEYS] ?? ROUTING_KEYS.indeterminate)}
          {data.routing_reason
            ? ` — ${i18nT('pages.overview.acpBackend.routing_reason', { reason: data.routing_reason })}`
            : ''}
        </p>
      )}
      {data.allow_ungated_tools && (
        <p className="text-warn" role="status">
          {i18nT('pages.overview.acpBackend.ungated_opt_out')}
        </p>
      )}

      <div className="flex flex-col gap-[7px]">
        {selectable.map((row) => {
          const active = row.id === data.active
          const open = expanded === row.id
          return (
            <div
              key={row.id}
              className={`rounded-md border px-3 py-2.5 ${
                active ? 'border-accent bg-accent-subtle' : 'border-border bg-bg-elevated'
              }`}
            >
              <label
                htmlFor={`acp-backend-${row.id || 'kiro'}`}
                className="flex items-start gap-3 cursor-pointer"
              >
                <input
                  id={`acp-backend-${row.id || 'kiro'}`}
                  type="radio"
                  name="acp-backend"
                  className="mt-1 accent-accent"
                  checked={active}
                  disabled={saving}
                  onChange={() => choose(row)}
                  // Named explicitly rather than relying on the wrapping label:
                  // the visible text is assembled from nested spans and a badge,
                  // so a screen reader would otherwise announce the row's full
                  // prose as the control's NAME. The backend name is what is
                  // being chosen; the rest is its description, linked below so it
                  // is still announced — never `aria-hidden`, which would hide
                  // the experimental warning from exactly the users who cannot
                  // see the badge.
                  aria-label={row.label}
                  aria-describedby={`acp-backend-desc-${row.id}`}
                />
                <span className="flex-1 min-w-0">
                  <span className="flex items-center gap-2 flex-wrap">
                    <span className="font-semibold text-text-strong">{row.label}</span>
                    {row.experimental && (
                      <span className="inline-flex items-center gap-1 text-[11px] font-semibold px-1.5 py-px rounded-full bg-warn-subtle text-warn border border-warn/30">
                        <FlaskConical className="lucide-inline" />
                        {i18nT('pages.overview.acpBackend.experimental')}
                      </span>
                    )}
                    {/* Only `missing` earns a marker. `unknown` means the probe
                        could not tell, and an operator who HAS the adapter must
                        not be told to install it; `installed` needs no badge
                        because working is the unremarkable case. */}
                    {row.installed === 'missing' && (
                      <span className="inline-flex items-center gap-1 text-[11px] font-semibold px-1.5 py-px rounded-full bg-danger-subtle text-danger border border-danger/30">
                        <PackageX className="lucide-inline" />
                        {i18nT('pages.overview.acpBackend.not_installed')}
                      </span>
                    )}
                  </span>
                  <span className="block text-[12px] text-muted mt-1" id={`acp-backend-desc-${row.id}`}>
                    {row.degraded_count === 0
                      ? i18nT('pages.overview.acpBackend.all_features_supported')
                      : i18nT('pages.overview.acpBackend.features_differ', {
                          count: row.degraded_count,
                          total: Object.keys(row.capabilities).length,
                        })}
                  </span>
                </span>
              </label>

              {row.degraded_count > 0 && (
                <>
                  <Btn
                    type="button"
                    className="mt-2 border-none px-0 text-[12px] text-muted hover:bg-transparent"
                    aria-expanded={open}
                    onClick={() => setExpanded(open ? null : row.id)}
                  >
                    {open ? (
                      <ChevronDown className="lucide-inline" />
                    ) : (
                      <ChevronRight className="lucide-inline" />
                    )}
                    {i18nT('pages.overview.acpBackend.show_what_changes')}
                  </Btn>
                  {open && (
                    <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 rounded border border-border bg-bg px-3 py-2 max-[600px]:grid-cols-1">
                      {Object.entries(row.capabilities).map(([cap, level]) => (
                        <div key={cap} className="flex justify-between gap-3 text-[12px]">
                          <span className="text-muted">{capabilityLabel(cap)}</span>
                          <span className={LEVEL_CLASS[level] ?? 'text-muted'}>
                            {levelLabel(level)}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
          )
        })}
      </div>

      <p className="text-[12px] text-muted mt-2.5">
        {i18nT('pages.overview.acpBackend.applies_to_new_sessions')}
      </p>

      {withheld.length > 0 && (
        <a
          className="block text-[12px] text-muted mt-1.5 hover:text-text hover:underline"
          href={ACP_BACKEND_DOCS}
          target="_blank"
          rel="noreferrer"
        >
          {i18nT('pages.overview.acpBackend.not_enabled_in_this_build', {
            backends: withheld.map((b) => b.label).join(', '),
          })}
        </a>
      )}

      <Modal
        open={pending !== null}
        onClose={() => setPending(null)}
        title={
          <span className="flex items-center gap-2">
            <AlertTriangle className="lucide-inline text-warn" />
            {i18nT('pages.overview.acpBackend.confirm_title', { backend: pending?.label ?? '' })}
          </span>
        }
        maxWidth={520}
        footer={
          <div className="flex gap-2 justify-end">
            <Btn
              type="button"
              onClick={() => setPending(null)}
            >
              {i18nT('pages.overview.acpBackend.cancel')}
            </Btn>
            <Btn
              type="button"
              primary
              disabled={saving}
              onClick={() => pending && void commit(pending)}
            >
              {i18nT('pages.overview.acpBackend.confirm_switch')}
            </Btn>
          </div>
        }
      >
        {pending && (
          <div className="text-[13px] flex flex-col gap-2">
            {pending.experimental && (
              <p>
                {i18nT('pages.overview.acpBackend.confirm_body', {
                  count: pending.degraded_count,
                  total: Object.keys(pending.capabilities).length,
                })}
              </p>
            )}
            <p>
              {i18nT('pages.overview.acpBackend.confirm_model_reset')}
            </p>
            {pending.experimental && (
              <>
                <ul className="list-disc pl-5 text-muted text-[12.5px] flex flex-col gap-0.5">
                  {Object.entries(pending.capabilities)
                    .filter(([, level]) => level !== 'supported')
                    .map(([cap, level]) => (
                      <li key={cap}>
                        {capabilityLabel(cap)} — {levelLabel(level)}
                      </li>
                    ))}
                </ul>
                <p className="text-muted">
                  {i18nT('pages.overview.acpBackend.confirm_prerequisites')}
                </p>
                {/* An ORDERED list, and install before sign-in: signing in needs the
                    adapter's own CLI present, so the reverse order sends the
                    operator to a command that does not exist yet. Rendered as
                    copyable code rather than prose for the same reason the sign-in
                    line always was — an operator pastes this. */}
                <ol className="list-decimal pl-5 text-[12.5px] flex flex-col gap-1">
                  {pending.install_command && pending.installed !== 'installed' && (
                    <li>
                      <code className="font-mono text-[12px] bg-bg-elevated px-1.5 py-px rounded">
                        {pending.install_command}
                      </code>
                    </li>
                  )}
                  <li>
                    <code className="font-mono text-[12px] bg-bg-elevated px-1.5 py-px rounded">
                      {pending.signin_command}
                    </code>
                  </li>
                </ol>
              </>
            )}
            <p className="text-muted">
              {i18nT('pages.overview.acpBackend.confirm_open_sessions')}
            </p>
          </div>
        )}
      </Modal>
    </Card>
  )
}
