import { onTestFinished, vi } from 'vitest'

/**
 * `crypto` as a page gets it over plain HTTP from another machine. That is not
 * a secure context, so `randomUUID` is absent while `getRandomValues` stays.
 * The bytes count up from zero, so the ids a test reads are the same on every
 * run. The real `crypto` is back when the calling test finishes.
 */
export function stubPlainHttpCrypto(): void {
  const real = globalThis.crypto
  let next = 0
  vi.stubGlobal('crypto', {
    getRandomValues: <T extends Uint8Array>(bytes: T): T => {
      for (let i = 0; i < bytes.length; i++) bytes[i] = next++ & 0xff
      return bytes
    },
  })
  onTestFinished(() => { vi.stubGlobal('crypto', real) })
}
