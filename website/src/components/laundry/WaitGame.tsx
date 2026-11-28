import { useEffect, useId, useRef, useState, type CSSProperties } from 'react'
import { useTranslation } from 'react-i18next'
import { RotateCcw } from 'lucide-react'
import { Btn } from '../ui'
import { fmtNumber } from '../../i18n/format'
import { isLoopbackHost } from '../../lib/loopbackHost'
import { FOCUSABLE } from '../../hooks/useDialogFocusTrap'
import { deepActiveElement } from '../../utils/editableTarget'
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
  const short = useShortWindow()
  // A card sits inside a dialog that scrolls: in a window this short the footer would cut the scene, so it offers none,
  // and Space is left alone there rather than eaten by a game that does not appear.
  const offered = local && !(surface === 'card' && short)
  useEffect(() => {
    if (shown || !offered) return
    const timer = window.setTimeout(() => setShown(true), WAIT_GAME_DELAY_MS)
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== ' ' || e.repeat || e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented) return
      // The key's own composed target: inside a shadow root `document.activeElement` is only the host.
      const t = e.composedPath?.()[0]
      const a = t instanceof HTMLElement ? t : deepActiveElement()
      // Space starts the game early only when it would not press, type into or scroll a control the person is on:
      // any focus stop counts, the same set a dialog's focus trap walks.
      if (a instanceof HTMLElement && a !== document.body && (a.isContentEditable || a.closest(FOCUSABLE) || scrolls(a))) return
      e.preventDefault()
      setShown(true)
    }
    // Capture phase: a host that stops keys from bubbling (the update dialog does) must not hide Space from this.
    window.addEventListener('keydown', onKey, true)
    return () => { window.clearTimeout(timer); window.removeEventListener('keydown', onKey, true) }
  }, [shown, offered])
  return shown && offered ? <LaundryGame surface={surface} className={className} /> : null
}

/** A focused region with content past its edge: Space scrolls it (browsers make a scroller a focus stop with no tabindex). */
function scrolls(el: HTMLElement) {
  return el.scrollHeight > el.clientHeight && /auto|scroll/.test(getComputedStyle(el).overflowY)
}

function useShortWindow() {
  const query = '(max-height: 480px)'
  const [short, setShort] = useState(() => typeof window.matchMedia === 'function' && window.matchMedia(query).matches)
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const mq = window.matchMedia(query)
    const on = () => setShort(mq.matches)
    on()
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])
  return short
}

function LaundryGame({ surface, className }: { surface: 'bg' | 'card'; className: string }) {
  const { t, i18n } = useTranslation()
  const rootRef = useRef<HTMLDivElement>(null)
  const handle = useRef<LaundryHandle | null>(null)
  const helpId = useId()
  // Read by the engine at the moment it shows or speaks a line, so a language
  // switch reaches the next message without restarting the game.
  const text = useRef<LaundryText>(null as unknown as LaundryText)
  text.current = {
    folded: (n: number) => t('app.laundry_stacked', { value: fmtNumber(n) }),
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
    pile: t('app.laundry_pile'),
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
  // The engine wrote its counter and item labels once; a language switch (also one made in another tab) rewrites them.
  // After the render, so `text.current` already holds the new language's strings.
  useEffect(() => { handle.current?.retext() }, [i18n.language])
  const resetGame = t('app.laundry_reset_game')
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
      <div className="laundry-chrome">
        <div className="laundry-hud">
          <p className="laundry-count" />
          <Btn className="min-h-9 gap-1.5" aria-keyshortcuts="R" onClick={() => handle.current?.restart()}>
            <RotateCcw size={16} aria-hidden="true" /> {resetGame}
          </Btn>
        </div>
        <p className="laundry-hint" aria-hidden="true">{t('app.laundry_hint')}</p>
      </div>
      <p className="laundry-toast" aria-hidden="true" />
      <div className="laundry-caption">
        <p className="laundry-caption-hint">{t('app.laundry_pointer_hint')}</p>
      </div>
      <p className="laundry-sr laundry-live" aria-live="polite" />
    </div>
  )
}
