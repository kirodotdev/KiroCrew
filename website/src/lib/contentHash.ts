/**
 * djb2 string hash → base36. A compact, stable, non-cryptographic fingerprint
 * for cache keys and dedup keys (NOT for security). Shared so call sites reuse
 * one implementation rather than re-inlining the `h = 5381` loop.
 */
export function contentHash(text: string): string {
  let h = 5381
  for (let i = 0; i < text.length; i++) h = ((h << 5) + h + text.charCodeAt(i)) | 0
  return (h >>> 0).toString(36)
}
