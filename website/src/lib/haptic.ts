// One haptic tap, best effort. Android: `navigator.vibrate`. iOS 18+ Safari and
// PWAs expose no vibration API, but clicking a hidden `<input type=checkbox switch>`
// fires the Taptic engine -- the same trick every haptic npm package wraps.
// Everywhere else (desktop, Windows, jsdom) this is a silent no-op, and it never
// throws: feedback must not break the action it decorates.
export type HapticKind = 'light' | 'medium' | 'success' | 'error'

const VIBRATE_MS: Record<HapticKind, number | number[]> = {
  light: 10,
  medium: 20,
  success: [10, 40, 10],
  error: [30, 40, 30],
}

// The hidden host and the control inside it. The control is what gets clicked:
// clicking the host would only reach it by way of label activation, which not
// every engine runs once the click's propagation has been stopped (below).
let iosSwitch: HTMLLabelElement | null = null
let iosInput: HTMLInputElement | null = null

// The engine behind the trick is the `switch` attribute (WebKit, iOS 17.4+):
// an engine that renders it also reflects it as a property on the element, so
// the probe is the feature itself, not the user agent. A UA that matches but
// lacks the property (an older iOS, a spoofed desktop) is treated as having no
// engine, which keeps the hidden control out of its document.
function hasIOSSwitch(): boolean {
  return /iPhone|iPad|iPod/.test(navigator.userAgent)
    && !('vibrate' in navigator)
    && 'switch' in document.createElement('input')
}

// The click the trick needs is a hardware trigger, not a tap on the page, but
// it travels the same path as one: capture from `window` down to the hidden
// control, then back up. Every click-outside closer and click swallower in the
// app would read it as the user's (a drawer's one-shot swallower would spend
// itself on it, and the finger's real click would then land on whatever it
// started over). So it is fenced at the first stop on that path: a capture
// listener on `window`, registered at import so it precedes any listener a
// component adds later. Stopping propagation does not cancel the control's
// own activation, which is what fires the engine.
function fenceSyntheticClick(e: Event): void {
  if (iosSwitch && iosSwitch.contains(e.target as Node | null)) e.stopImmediatePropagation()
}
if (typeof window !== 'undefined') window.addEventListener('click', fenceSyntheticClick, { capture: true })

function iosTap(): void {
  if (!iosSwitch || !iosInput) {
    iosSwitch = document.createElement('label')
    iosSwitch.setAttribute('aria-hidden', 'true')
    Object.assign(iosSwitch.style, { position: 'fixed', width: '0', height: '0', overflow: 'hidden', opacity: '0', pointerEvents: 'none' })
    iosInput = document.createElement('input')
    iosInput.type = 'checkbox'
    iosInput.setAttribute('switch', '')
    // Hidden from assistive tech above, so it must not be a tab stop either.
    iosInput.tabIndex = -1
    iosSwitch.appendChild(iosInput)
    document.body.appendChild(iosSwitch)
  }
  iosInput.click()
}

export function haptic(kind: HapticKind = 'light'): void {
  if (typeof navigator === 'undefined') return
  try {
    if ('vibrate' in navigator) navigator.vibrate(VIBRATE_MS[kind])
    else if (hasIOSSwitch()) iosTap()
  } catch {
    // no haptics on this device
  }
}
