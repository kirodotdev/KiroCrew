/**
 * The initials avatar beside a delivered message's sender title, in the colour
 * the person gave THAT session (Sessions sidebar): its custom `color_hex`, else
 * its `color_index` in the current palette (`utils/sessionColors`). A session
 * with no colour, or a host with no store, draws a neutral circle. Decorative:
 * the title beside it carries the name.
 */
import { useContext, useMemo, useSyncExternalStore } from 'react'
import { ReactReduxContext } from 'react-redux'

import { senderInitials } from '../../lib/crewSeed'
import { useOptionalTheme } from '../../hooks/useTheme'
import { generatePalette, getContrastingTextColor, type PaletteName } from '../../utils/sessionColors'

const noopSubscribe = () => () => {}

interface ColorRow { key: string; color_hex?: string | null; color_index?: number | null }

/** `hex|index|palette` of *senderKey*'s session colour, subscribed to the store. */
function useSenderColorSig(senderKey: string): string {
  const redux = useContext(ReactReduxContext)
  const subscribe = redux ? redux.store.subscribe : noopSubscribe
  return useSyncExternalStore(subscribe, () => {
    const dash = (redux?.store.getState() as { dashboard?: { slots?: ColorRow[]; sessionColorsPalette?: string } } | undefined)?.dashboard
    const row = dash?.slots?.find(r => r.key === senderKey)
    const hex = typeof row?.color_hex === 'string' && /^#[0-9a-f]{6}$/i.test(row.color_hex) ? row.color_hex : ''
    const index = typeof row?.color_index === 'number' && row.color_index >= 0 ? String(row.color_index) : ''
    return `${hex}|${index}|${dash?.sessionColorsPalette || ''}`
  })
}

export default function SenderAvatar({ senderKey, title }: { senderKey: string; title: string }) {
  const sig = useSenderColorSig(senderKey)
  const theme = useOptionalTheme()
  const themeVersion = theme?.themeVersion
  const fill = useMemo(() => {
    const [hex, index, palette] = sig.split('|')
    if (hex) return hex
    if (!index) return null
    // The same seed the sidebar derives its palette from (`useSessionPalette`).
    const cs = getComputedStyle(document.documentElement)
    const seed = cs.getPropertyValue('--accent-subtle').trim() || cs.getPropertyValue('--accent').trim()
    if (!seed) return null
    return generatePalette(seed, (palette || 'horizon') as PaletteName, cs.getPropertyValue('--bg-accent').trim())[Number(index)] ?? null
    // themeVersion re-derives the palette when the theme changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig, themeVersion])
  return (
    <span
      className={`inline-flex items-center justify-center align-middle w-5 h-5 mr-1 rounded-full text-[10px] font-semibold normal-case tracking-normal tabular-nums ${fill ? '' : 'bg-border text-text-strong'}`}
      style={fill ? { background: fill, color: getContrastingTextColor(fill) } : undefined}
      aria-hidden="true"
      data-testid="session-seed-sender-avatar"
      data-session-color={fill ? 'true' : 'false'}
    >
      {senderInitials(title)}
    </span>
  )
}
