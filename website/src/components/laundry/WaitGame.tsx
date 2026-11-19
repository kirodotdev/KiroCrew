import { useEffect, useId, useRef, useState, type CSSProperties } from 'react'
import { useTranslation } from 'react-i18next'
import { RotateCcw } from 'lucide-react'
import { Btn } from '../ui'
import { fmtNumber } from '../../i18n/format'
import { isLoopbackHost } from '../../lib/loopbackHost'
import { FOCUSABLE } from '../../hooks/useDialogFocusTrap'
import { startLaundry, type LaundryHandle, type LaundryText } from './engine'
import './laundry.css'

/** How long a wait surface stays up before the game appears on its own. */
export const WAIT_GAME_DELAY_MS = 30_000


/**
 * Something to do while the dashboard waits for its gateway: a laundry-folding
 * game, the way a browser offers a game while the network is down.
 *
 * One component, rendered by every surface that waits on the gateway on this
 * machine (the host-offline cover, the in-app update's reconnect step, the
 * update dialog's restart). Each surface keeps its own status text above it.
 * The game appears after `WAIT_GAME_DELAY_MS`, or at once when the person
 * presses Space while no control has focus, and goes away with the surface.
 * Nothing renders before then, so a short wait looks exactly as it did.
 *
 * Only on this machine's own dashboard (a loopback origin, which includes the
 * desktop app), the same rule as the host-offline cover: a dashboard reached
 * through a tunnel or another name offers no game.
 *
 * `surface` names the background the game sits on, so its drawings fill with
 * the same colour as the page (`bg`) or card (`card`) around them.
 */
export function WaitGame({ surface = 'bg', className = '' }: { surface?: 'bg' | 'card'; className?: string }) {
  const [shown, setShown] = useState(false)
  const local = isLoopbackHost(window.location.hostname)
  useEffect(() => {
    if (shown || !local) return
    const timer = window.setTimeout(() => setShown(true), WAIT_GAME_DELAY_MS)
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== ' ' || e.repeat || e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented) return
      const a = document.activeElement
      // Space starts the game early only when it would not press, type into or scroll a control the person is on:
      // any focus stop counts, the same set a dialog's focus trap walks.
      if (a instanceof HTMLElement && a !== document.body && (a.isContentEditable || a.closest(FOCUSABLE))) return
      e.preventDefault()
      setShown(true)
    }
    // Capture phase: a host that stops keys from bubbling (the update dialog does) must not hide Space from this.
    window.addEventListener('keydown', onKey, true)
    return () => { window.clearTimeout(timer); window.removeEventListener('keydown', onKey, true) }
  }, [shown, local])
  return shown && local ? <LaundryGame surface={surface} className={className} /> : null
}

function LaundryGame({ surface, className }: { surface: 'bg' | 'card'; className: string }) {
  const { t } = useTranslation()
  const rootRef = useRef<HTMLDivElement>(null)
  const handle = useRef<LaundryHandle | null>(null)
  const helpId = useId()
  // Read by the engine at the moment it shows or speaks a line, so a language
  // switch reaches the next message without restarting the game.
  const text = useRef<LaundryText>(null as unknown as LaundryText)
  text.current = {
    digits: (n: number) => fmtNumber(n, { useGrouping: false }),
    folded: (n: number) => t('app.laundry_folded', { value: fmtNumber(n) }),
    item_basket: t('app.laundry_item_basket'),
    item_table: t('app.laundry_item_table'),
    item_part_folded: t('app.laundry_item_part_folded'),
    item_folded: t('app.laundry_item_folded'),
    placed_table: t('app.laundry_placed_table'),
    placed_basket: t('app.laundry_placed_basket'),
    placed_pile: t('app.laundry_placed_pile'),
    fold_done: t('app.laundry_fold_done'),
    fold_step: t('app.laundry_fold_step'),
    fold_first: t('app.laundry_fold_first'),
    table_full: t('app.laundry_table_full'),
    picked_up: t('app.laundry_picked_up'),
    put_back: t('app.laundry_put_back'),
    over_basket: t('app.laundry_over_basket'),
    over_table: t('app.laundry_over_table'),
    over_pile: t('app.laundry_over_pile'),
    ghost: t('app.laundry_ghost'),
    pile_full: t('app.laundry_pile_full'),
    started_over: t('app.laundry_started_over'),
  }
  useEffect(() => {
    const root = rootRef.current
    if (!root) return
    const h = startLaundry(root, { text: () => text.current })
    handle.current = h
    return () => { h.stop(); handle.current = null }
  }, [])
  const startOver = t('app.laundry_start_over')
  return (
    <div
      ref={rootRef}
      data-testid="wait-game"
      className={`laundry-game ${className}`}
      style={{ '--paper': surface === 'card' ? 'var(--card)' : 'var(--bg)' } as CSSProperties}
      tabIndex={-1}
      role="group"
      aria-label={t('app.laundry_label')}
      aria-describedby={helpId}
    >
      <canvas aria-hidden="true" />
      <p className="laundry-sr" id={helpId}>{t('app.laundry_help')}</p>
      <div className="laundry-keys" />
      <p className="laundry-count" />
      <Btn className="laundry-restart h-9 w-9 justify-center rounded-full px-0 py-0" aria-label={startOver} aria-keyshortcuts="R" title={startOver} onClick={() => handle.current?.restart()}>
        <RotateCcw size={18} aria-hidden="true" />
      </Btn>
      <p className="laundry-hint" aria-hidden="true">{t('app.laundry_hint')}</p>
      <p className="laundry-toast" aria-hidden="true" />
      <p className="laundry-caption">{t('app.laundry_caption')}</p>
      <p className="laundry-sr laundry-live" aria-live="polite" />
    </div>
  )
}
