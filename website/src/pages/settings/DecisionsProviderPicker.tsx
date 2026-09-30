import { useId, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink } from 'lucide-react'

import { api } from '../../api/client'
import { isNotFoundError } from '../../api/apiError'
import type { DecisionsLocalModel, DecisionsProviderData } from '../../api/client/decisions'
import { CodeBlock } from '../../components/CodeBlock'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { fmtNumber, fmtPercent, fmtUnit } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/** Preset id for hosted Jev; the gateway's `local_models.PRESET_JEV`. */
export const PRESET_JEV = 'jev'

/** Loopback ports the provider route accepts; the gateway's `PORT_MIN`/`PORT_MAX`. */
const PORT_MIN = 1024
const PORT_MAX = 65535

export const DECISIONS_PROVIDER_QUERY_KEY = ['decisionsProvider'] as const

/**
 * The preset recommended for a machine with `totalGb` of memory: the first local
 * preset, in the gateway's order, whose threshold the machine meets, else hosted
 * Jev. An unknown size recommends Jev, because suggesting a model the machine
 * cannot hold is worse than suggesting nothing local.
 */
export function recommendedPreset(presets: DecisionsLocalModel[], totalGb: number | null | undefined): string {
  if (typeof totalGb !== 'number' || !Number.isFinite(totalGb) || totalGb <= 0) return PRESET_JEV
  return presets.find(p => totalGb >= p.recommended_total_ram_gb)?.id ?? PRESET_JEV
}

/** The explicit port of *endpoint*, or `null`. */
function portOf(endpoint: string): number | null {
  try {
    const raw = new URL(endpoint).port
    return raw === '' ? null : portFrom(raw)
  } catch {
    return null
  }
}

function portFrom(draft: string): number | null {
  if (!/^\d+$/.test(draft.trim())) return null
  const port = Number(draft)
  return port >= PORT_MIN && port <= PORT_MAX ? port : null
}

/**
 * Which System One server answers the seam: hosted Jev, or a model on this machine.
 *
 * Each local preset states, in the reader's terms, how close it comes to Jev, what
 * memory it needs and how slow it is, and the card marks the one this machine's
 * memory suits. Choosing one writes a preset id and a port -- never an address --
 * through the owner-only provider route, which builds the loopback URL itself.
 */
export function DecisionsProviderPicker({ frozen }: { frozen: boolean }) {
  const qc = useQueryClient()
  const headingId = useId()
  const providerQ = useQuery<DecisionsProviderData>({
    queryKey: DECISIONS_PROVIDER_QUERY_KEY,
    queryFn: () => api.getDecisionsProvider(),
    retry: false,
  })
  // Total memory only: a server holds its weights resident, so what the machine
  // HAS decides whether a model fits, not what happens to be free this minute.
  // `null`, not `undefined`, for "no figure": react-query refuses an undefined result.
  const memQ = useQuery<number | null>({
    queryKey: ['decisionsHostMemory'],
    queryFn: () => api.system().then(d => (typeof d.mem_total_gb === 'number' ? d.mem_total_gb : null)),
    staleTime: 5 * 60_000,
  })
  const [selected, setSelected] = useState<string | null>(null)
  const [portDraft, setPortDraft] = useState<string | null>(null)
  const saveMut = useMutation({
    mutationFn: ({ preset, port }: { preset: string; port?: number }) => api.saveDecisionsProvider(preset, port),
    onSuccess: () => {
      setSelected(null)
      setPortDraft(null)
    },
    // The endpoint moved, so the consent row's "sent to" line and the config read
    // both change with it.
    onSettled: () =>
      Promise.all([
        qc.invalidateQueries({ queryKey: DECISIONS_PROVIDER_QUERY_KEY }),
        qc.invalidateQueries({ queryKey: ['decisionsConsent'] }),
        qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
      ]),
  })

  const data = providerQ.data
  // An older gateway has no provider route: that 404 is an answer, not a failure,
  // and the card's own pointer already says where the address is set, so nothing
  // is drawn. Any other failed read says so, with the hand-off -- there is no draft
  // to lose before the list has loaded.
  if (providerQ.isError) {
    return isNotFoundError(providerQ.error) ? null : (
      <ErrorNotice
        variant="inline"
        askAgent
        message={i18nT('pages.developer.featurePreviewsTab.decisions_provider_unavailable')}
      />
    )
  }
  if (!data) return null

  const presets = data.presets
  const totalGb = memQ.data
  const recommended = recommendedPreset(presets, totalGb)
  const chosen = selected ?? (data.active === 'custom' ? '' : data.active)
  const chosenPreset = presets.find(p => p.id === chosen)
  // The preset in use starts from the port its configured address names, so a
  // reload after saving 9100 shows 9100 rather than the preset's default.
  const configuredPort = chosenPreset && chosen === data.active ? portOf(data.configured_endpoint) : null
  const shownPort = portDraft ?? String(configuredPort ?? chosenPreset?.default_port ?? '')
  const port = chosenPreset ? portFrom(shownPort) : null
  const needsSave = chosen !== '' && (chosen !== data.active || (chosenPreset !== undefined && portDraft !== null))
  const canSave = needsSave && !frozen && !saveMut.isPending && (chosenPreset === undefined || port !== null)

  const recommendedBadge = (id: string) =>
    id === recommended && typeof totalGb === 'number' ? (
      <span className="rounded bg-accent/15 px-1.5 text-[11px] text-accent">
        {i18nT('pages.developer.featurePreviewsTab.decisions_provider_recommended', {
          memory: fmtUnit(totalGb, 'gigabyte', { maximumFractionDigits: 0 }),
        })}
      </span>
    ) : null
  const activeBadge = (id: string) =>
    id === data.active ? (
      <span className="rounded bg-bg px-1.5 text-[11px] text-muted border border-border">
        {i18nT('pages.developer.featurePreviewsTab.decisions_provider_active')}
      </span>
    ) : null

  const option = (id: string, name: string, details: ReactNode) => (
    <label
      key={id}
      className={`flex items-start gap-2 rounded-md border px-2.5 py-1.5 cursor-pointer ${
        chosen === id ? 'border-accent bg-bg' : 'border-border bg-bg'
      }`}
    >
      <input
        type="radio"
        name={headingId}
        className="mt-1"
        checked={chosen === id}
        disabled={frozen || saveMut.isPending}
        onChange={() => {
          setSelected(id)
          setPortDraft(null)
        }}
      />
      <span className="flex flex-col gap-0.5 min-w-0">
        <span className="flex flex-wrap items-center gap-1.5 text-[12px] font-medium text-text">
          {name}
          {activeBadge(id)}
          {recommendedBadge(id)}
        </span>
        {details}
      </span>
    </label>
  )

  return (
    <div className="flex flex-col gap-1.5" role="radiogroup" aria-labelledby={headingId}>
      <p id={headingId} className="text-[12px] font-medium text-text m-0">
        {i18nT('pages.developer.featurePreviewsTab.decisions_provider_label')}
      </p>
      <p className="text-[12px] text-muted m-0">{i18nT('pages.developer.featurePreviewsTab.decisions_provider_desc')}</p>
      {option(
        PRESET_JEV,
        i18nT('pages.developer.featurePreviewsTab.decisions_provider_jev'),
        <span className="text-[12px] text-muted">
          {i18nT('pages.developer.featurePreviewsTab.decisions_provider_jev_detail')}
        </span>,
      )}
      {presets.map(p =>
        option(
          p.id,
          p.name,
          <>
            <span className="text-[12px] text-text">
              {i18nT('pages.developer.featurePreviewsTab.decisions_provider_quality', {
                percent: fmtPercent(p.jev_relative_pct / 100, { maximumFractionDigits: 0 }),
                hard: fmtPercent(p.hard_relative_pct / 100, { maximumFractionDigits: 0 }),
              })}
            </span>
            <span className="text-[12px] text-muted">
              {i18nT('pages.developer.featurePreviewsTab.decisions_provider_memory', {
                peak: fmtUnit(p.peak_ram_gb, 'gigabyte', { maximumFractionDigits: 0 }),
                total: fmtUnit(p.recommended_total_ram_gb, 'gigabyte', { maximumFractionDigits: 0 }),
              })}
            </span>
            <span className="text-[12px] text-muted">
              {i18nT('pages.developer.featurePreviewsTab.decisions_provider_speed', {
                p50: fmtUnit(p.p50_secs, 'second', { maximumFractionDigits: 1 }),
                p95: fmtUnit(p.p95_secs, 'second', { maximumFractionDigits: 1 }),
              })}
            </span>
          </>,
        ),
      )}
      {chosenPreset && (
        <div className="flex flex-col gap-1 rounded-md border border-border bg-bg-accent px-2.5 py-1.5">
          <label className="flex items-center gap-2 text-[12px] text-text">
            {i18nT('pages.developer.featurePreviewsTab.decisions_provider_port')}
            <input
              type="text"
              inputMode="numeric"
              className="w-24 rounded border border-border bg-bg px-2 py-0.5 text-[12px] text-text font-mono"
              value={shownPort}
              disabled={frozen || saveMut.isPending}
              aria-invalid={port === null}
              onChange={e => setPortDraft(e.target.value)}
            />
          </label>
          {port === null && (
            <p role="alert" className="text-[12px] text-warn m-0">
              {i18nT('pages.developer.featurePreviewsTab.decisions_provider_port_invalid', {
                min: fmtNumber(PORT_MIN, { useGrouping: false }),
                max: fmtNumber(PORT_MAX, { useGrouping: false }),
              })}
            </p>
          )}
          <p className="text-[12px] text-muted m-0">{i18nT('pages.developer.featurePreviewsTab.decisions_provider_start')}</p>
          <CodeBlock
            code={chosenPreset.serve_command.replace(/\{port\}/g, port === null ? String(chosenPreset.default_port) : String(port))}
            lang="bash"
            complete
          />
          <a
            href={chosenPreset.setup_doc}
            target="_blank"
            rel="noreferrer noopener"
            className="inline-flex items-center gap-1 text-[12px] text-accent hover:underline w-fit"
          >
            {i18nT('pages.developer.featurePreviewsTab.decisions_provider_setup')}
            <ExternalLink size={11} aria-hidden="true" />
          </a>
        </div>
      )}
      <p className="text-[11px] text-muted m-0">{i18nT('pages.developer.featurePreviewsTab.decisions_provider_measured')}</p>
      {needsSave && (
        <div>
          <Btn
            disabled={!canSave}
            onClick={() =>
              saveMut.mutate(chosenPreset ? { preset: chosen, port: port ?? undefined } : { preset: chosen })
            }
          >
            {i18nT('pages.developer.featurePreviewsTab.decisions_provider_use')}
          </Btn>
        </div>
      )}
      {/* No hand-off: a failed save is when the port field may hold a draft the
          gateway has not taken, and asking the agent unmounts this card. */}
      {saveMut.isError && (
        <ErrorNotice variant="inline" message={i18nT('pages.developer.featurePreviewsTab.decisions_provider_save_failed')} />
      )}
    </div>
  )
}
