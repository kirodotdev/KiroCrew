/** The launch form's advanced "custom instance type" choice.
 *
 *  Lists the EC2 instance types the chosen Region OFFERS for a Crew host
 *  (`GET /api/cloud/instance-types`) and lets the reader pick one instead of a
 *  size tier. The list is read only once the section is opened. Its one hint
 *  says what the reader cannot see from the list: the 16 GB floor, and that a
 *  listed type is not necessarily in stock. The gateway checks the pick again
 *  when the launch is sent. */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ExternalLink } from 'lucide-react'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { reportForError } from '../../utils/errorReport'
import { errMessage } from '../../utils/thunkError'

/** AWS's on-demand price list: a custom type has no price of its own in the form. */
export const EC2_ON_DEMAND_PRICING_URL = 'https://aws.amazon.com/ec2/pricing/on-demand/'
/** How long one Region's list is reused in this tab (the gateway caches too). */
const LIST_STALE_MS = 5 * 60_000

export function CustomInstanceTypePicker({ profile, region, value, onPick }: {
  profile: string
  region: string
  /** The picked type; '' means the size tiers drive the launch. */
  value: string
  onPick: (instanceType: string) => void
}) {
  const [open, setOpen] = useState(value !== '')
  const [filter, setFilter] = useState('')
  const regionTrimmed = region.trim()
  const query = useQuery({
    queryKey: ['cloud', 'instance-types', profile, regionTrimmed],
    queryFn: () => api.cloudInstanceTypes(profile, regionTrimmed),
    enabled: open && regionTrimmed !== '',
    staleTime: LIST_STALE_MS,
    retry: false,
  })
  const needle = filter.trim().toLowerCase()
  const rows = (query.data?.instance_types ?? []).filter(r => r.instance_type.includes(needle))

  return (
    <div className="mt-2.5">
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
        className="w-full flex items-center gap-2 text-[12px] text-muted px-3 py-2 rounded-md border border-dashed border-border-strong hover:text-text"
      >
        <ChevronDown size={14} aria-hidden="true" className={`transition-transform ${open ? 'rotate-180' : ''}`} /> {i18nT('pages.settings.remoteCrewPanel.custom_type')}
        {/* No size card is chosen while a type is picked, so the toggle names it:
            collapsed, it is the only place on the form that says what launches. */}
        {value !== '' && <code translate="no" className="font-mono text-text-strong">{value}</code>}
      </button>
      {open && (
        <div className="mt-2 space-y-2">
          <p className="text-[12px] text-muted">{i18nT('pages.settings.remoteCrewPanel.custom_type_hint')}</p>
          {/* The rows carry no price, so the link sits above them: the reader needs
              it while choosing, not after. */}
          <a
            className="text-[12px] text-accent hover:underline inline-flex items-center gap-1"
            href={EC2_ON_DEMAND_PRICING_URL}
            target="_blank"
            rel="noreferrer"
          >
            {i18nT('pages.settings.remoteCrewPanel.custom_type_price')} <ExternalLink size={12} aria-hidden="true" />
          </a>
          {regionTrimmed === '' ? (
            <p className="text-[12px] text-muted">{i18nT('pages.settings.remoteCrewPanel.custom_type_need_region')}</p>
          ) : query.isPending ? (
            <p className="text-[12px] text-muted" role="status">{i18nT('pages.settings.remoteCrewPanel.custom_type_loading', { region: regionTrimmed })}</p>
          ) : query.isError ? (
            /* askAgent ON: the launch form keeps every entry the reader has not
               launched with (crewForms.launch), the picked type included, so the
               hand-off loses nothing; a list AWS would not give is one the agent
               can look into. */
            <ErrorNotice
              variant="inline"
              askAgent
              message={i18nT('pages.settings.remoteCrewPanel.custom_type_load_failed', { detail: errMessage(query.error) })}
              report={reportForError(query.error)}
            />
          ) : (
            <>
              <input
                type="search"
                value={filter}
                onChange={e => setFilter(e.target.value)}
                aria-label={i18nT('pages.settings.remoteCrewPanel.custom_type_filter')}
                placeholder={i18nT('pages.settings.remoteCrewPanel.custom_type_filter_placeholder')}
                spellCheck={false}
                className="w-full px-2 py-1.5 text-[13px] font-mono bg-bg border border-border rounded text-text outline-hidden focus-visible:border-accent"
              />
              {rows.length === 0 ? (
                <p className="text-[12px] text-muted">{i18nT('pages.settings.remoteCrewPanel.custom_type_none')}</p>
              ) : (
                <div
                  role="radiogroup"
                  aria-label={i18nT('pages.settings.remoteCrewPanel.custom_type')}
                  className="max-h-72 overflow-y-auto rounded-md border border-border divide-y divide-border"
                >
                  {rows.map(r => (
                    <label key={r.instance_type} className="flex items-center gap-2 px-3 py-2 text-[13px] text-text cursor-pointer hover:bg-bg-elevated">
                      {/* The native dot reads as disabled in dark mode; the ring is the
                          size cards' own, so an unpicked row looks pickable in both themes. */}
                      <input
                        type="radio"
                        className="appearance-none w-4 h-4 shrink-0 rounded-full border-[1.5px] border-border-strong checked:border-accent checked:bg-accent cursor-pointer focus-ring-accent"
                        name="custom-instance-type"
                        aria-label={r.instance_type}
                        checked={value === r.instance_type}
                        onChange={() => onPick(r.instance_type)}
                      />
                      <code translate="no" className="font-mono text-text-strong">{r.instance_type}</code>
                      <span className="text-[12px] text-muted">
                        {i18nT('pages.settings.remoteCrewPanel.custom_type_row', {
                          memory: fmtNumber(r.memory_gib, { maximumFractionDigits: 1 }),
                          vcpu: fmtNumber(r.vcpu),
                          cpu: i18nT(r.arch === 'arm64'
                            ? 'pages.settings.remoteCrewPanel.custom_type_cpu_arm'
                            : 'pages.settings.remoteCrewPanel.custom_type_cpu_x86'),
                          zones: fmtNumber(r.zones.length),
                        })}
                      </span>
                    </label>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}
