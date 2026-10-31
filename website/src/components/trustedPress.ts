// Capture the browser's brand-checking operations before an installed app's
// bundle runs in this document. Reading these properties normally would let a
// Proxy around a real Event lie from its `get` trap.
const apply = Reflect.apply
const isTrustedGetter = Object.getOwnPropertyDescriptor(
  new Event('trusted-press-brand-check'),
  'isTrusted',
)?.get
const targetGetter = Object.getOwnPropertyDescriptor(Event.prototype, 'target')?.get
const typeGetter = Object.getOwnPropertyDescriptor(Event.prototype, 'type')?.get
const contains = Node.prototype.contains

/**
 * Whether a press on `button` came from the user rather than from script.
 *
 * The captured accessors reject objects and Proxies through their browser brand
 * checks. The target must also be inside `button`, so a trusted click the app
 * caught on its own button cannot be replayed here.
 */
export function isTrustedPress(
  event: { nativeEvent?: unknown } | undefined,
  button: Element | null,
): boolean {
  const native = event?.nativeEvent
  if (!native || !button || !isTrustedGetter || !targetGetter || !typeGetter) return false
  try {
    if (apply(typeGetter, native, []) !== 'click') return false
    if (apply(isTrustedGetter, native, []) !== true) return false
    const target = apply(targetGetter, native, [])
    return !!target && apply(contains, button, [target])
  } catch {
    // Browser accessors throw when `this` is not their branded native object.
    return false
  }
}
