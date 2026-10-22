import { useState } from 'react'
import { KiroGhostMark } from './KiroGhostMark'

// Identity, not connection state. Hash the stable id rather than its position
// so reorder/removal/rename cannot recolour neighbours, and an embedded pane
// derives the same tint without a second persisted preference or host protocol.
const REMOTE_TINTS = ['--ok', '--info', '--warn', '--clarify', '--danger'] as const
export function crewIdentityTint(id: string | null): string {
  if (id === null) return 'var(--aim)'
  let hash = 0
  for (const char of id) hash = (Math.imul(hash, 31) + char.charCodeAt(0)) >>> 0
  return `var(${REMOTE_TINTS[hash % REMOTE_TINTS.length]})`
}

/** The product artwork identifies Local; the adjacent text supplies its name.
 *  `/logo.png` is gateway-served, not a bundled asset, so a 404, a blocked
 *  request or a hung fetch would otherwise leave the collapsed rail's only
 *  visible identity blank. Same load-proof contract as RailHeaderGlyph /
 *  MobileNavGlyph: a bundled KiroGhostMark fills the box by default, the swap
 *  to the logo happens only on the img's own `load`, and `error` reverts it.
 *  The img stays mounted (hidden) while the fallback shows so the browser still
 *  fetches it. */
export function LocalCrewIcon({ size = 36 }: { size?: number }) {
  const [loaded, setLoaded] = useState(false)
  return (
    <>
      {!loaded && (
        <span data-testid="local-crew-icon-fallback" aria-hidden="true" className="flex items-center justify-center shrink-0" style={{ width: size, height: size }}>
          <KiroGhostMark size={Math.round(size * 0.6)} />
        </span>
      )}
      <img src="/logo.png" alt="" aria-hidden="true" draggable={false}
        onLoad={() => setLoaded(true)} onError={() => setLoaded(false)}
        data-testid="local-crew-icon" width={size} height={size}
        className={`block shrink-0 object-contain${loaded ? '' : ' hidden'}`} />
    </>
  )
}

/** Supplemental identity; the adjacent name and tooltip identify the crew.
 *  `radius` overrides the corner rounding — the default `rounded-lg` reads right
 *  at the 36px app-icon size, but a smaller tile needs a smaller radius or the
 *  rounded square looks like a circle. Pass a Tailwind radius class to tune it. */
export default function CrewIdentityMark({ id, size = 36, radius = 'rounded-lg', surface = 'var(--chrome)' }: { id: string | null; size?: number; radius?: string; surface?: string }) {
  const color = crewIdentityTint(id)
  const inner = Math.round(size * 0.6)
  // `surface` is what the 12%/35% tints mix INTO. It defaults to --chrome, which
  // is correct inside the switcher menu (a --chrome-coloured surface). The nav
  // rail passes its own surface (--bg-accent) so the tile blends into the rail
  // in both light and dark themes.
  return (
    <span
      aria-hidden="true"
      data-testid="crew-identity-mark"
      style={id === null
        ? { width: size, height: size }
        : { width: size, height: size, color, background: `color-mix(in srgb, ${color} 12%, ${surface})`, borderColor: `color-mix(in srgb, ${color} 35%, var(--border))` }}
      className={`flex items-center justify-center ${radius} shrink-0${id === null ? '' : ' border'}`}
    >
      {id === null ? <LocalCrewIcon size={size} /> : <KiroGhostMark size={inner} />}
    </span>
  )
}
