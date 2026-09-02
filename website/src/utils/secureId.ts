/** Cryptographically-strong random id generation.
 *
 *  Prefers ``crypto.randomUUID()``, but that API is only defined in a **secure
 *  context** (HTTPS or localhost). When the dashboard is served over plain HTTP
 *  from a non-loopback address it is NOT a secure context, so ``randomUUID`` is
 *  absent and calling it throws. We fall back to ``crypto.getRandomValues()``,
 *  which is available in every context (secure or not) and is still a CSPRNG —
 *  so the fallback never degrades to the non-cryptographic ``Math.random()``.
 */

function uuidFromRandomBytes(): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  // RFC 4122 v4: set version (4) and variant (10xx) bits.
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex: string[] = []
  for (let i = 0; i < 256; i++) hex.push((i + 0x100).toString(16).slice(1))
  const b = bytes
  return (
    hex[b[0]] + hex[b[1]] + hex[b[2]] + hex[b[3]] + '-' +
    hex[b[4]] + hex[b[5]] + '-' +
    hex[b[6]] + hex[b[7]] + '-' +
    hex[b[8]] + hex[b[9]] + '-' +
    hex[b[10]] + hex[b[11]] + hex[b[12]] + hex[b[13]] + hex[b[14]] + hex[b[15]]
  )
}

/** Return a cryptographically-strong UUID v4 string, usable in any context. */
export function secureRandomId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return uuidFromRandomBytes()
}
