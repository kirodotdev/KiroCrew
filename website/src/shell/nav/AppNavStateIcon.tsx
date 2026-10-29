/**
 * AppNavStateIcon: an app's nav-rail icon with a separate file for the
 * not-selected state (`ui.pages[].iconInactiveUrl`).
 *
 * Built-in icons are inlined SVGs that repaint from theme tokens, so they grey
 * out on an unselected row by themselves. An installed app's icon is a plain
 * `<img>` and cannot, so an app that wants the same look ships a second file.
 *
 * Both variants render, and `index.css` shows one by the row's own state
 * (`.app-icon-nav.is-lit` and row hover), so the rail needs no per-row state.
 * Outside a nav row only the normal icon shows. If the inactive file fails to
 * load, the row falls back to the normal icon in every state rather than
 * showing a broken image.
 */
import { useEffect, useState, type ReactElement } from 'react'

export default function AppNavStateIcon({
  icon,
  inactiveSrc,
}: {
  /** The icon the row shows when selected, and everywhere outside the rail. */
  icon: ReactElement
  /** A URL already resolved and vetted by `appNav.ts` (`installedUiArt`). */
  inactiveSrc: string
}) {
  const [failed, setFailed] = useState(false)
  // A new file (an app update) gets a fresh try instead of a sticky failure.
  useEffect(() => { setFailed(false) }, [inactiveSrc])
  if (!inactiveSrc || failed) return icon
  return (
    <>
      {/* Sized from the wrapper so the normal icon fills the same box as the
          idle <img>: the touch rail enlarges only `.app-icon-nav`'s direct
          children, and the icon itself sits one level deeper. */}
      <span className="app-nav-icon-lit inline-flex w-full h-full items-center justify-center [&>*]:!w-full [&>*]:!h-full">{icon}</span>
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- onError is an image-load lifecycle handler (fall back to the normal icon), not a user interaction */}
      <img
        src={inactiveSrc}
        alt=""
        className="app-nav-icon-idle w-4 h-4 rounded-sm object-contain"
        onError={() => setFailed(true)}
      />
    </>
  )
}
