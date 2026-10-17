/**
 * Mochi shared utility functions.
 * These are pure functions — no Electron/Node dependencies.
 */

// ── Accessibility context truncation (Property 5) ─────────────────────────

const MAX_ACCESSIBILITY_CHARS = 2000

/**
 * Truncate Accessibility API context to MAX_ACCESSIBILITY_CHARS.
 * Property 5: result length never exceeds 2000 characters.
 */
export function truncateAccessibilityContext(raw: string): string {
  if (raw.length <= MAX_ACCESSIBILITY_CHARS) return raw
  return raw.slice(0, MAX_ACCESSIBILITY_CHARS)
}

export { MAX_ACCESSIBILITY_CHARS }

// ── Clipboard content filter (Property 6) ────────────────────────────────

const MAX_CLIPBOARD_CHARS = 10_000

/**
 * Returns true if clipboard content should trigger the "handle this?" prompt.
 * Property 6: content longer than 10000 chars is silently ignored.
 * Filters out content that looks like passwords, API keys, or tokens.
 */
export function shouldPromptForClipboard(content: string): boolean {
  if (content.length === 0 || content.length > MAX_CLIPBOARD_CHARS) return false
  // Filter out likely sensitive content
  if (isSensitiveContent(content)) return false
  return true
}

/** Detect content that looks like passwords, API keys, tokens, or secrets */
export function isSensitiveContent(text: string): boolean {
  const trimmed = text.trim()
  // Single-line strings that look like tokens/keys (high entropy, no spaces)
  if (!trimmed.includes('\n') && !trimmed.includes(' ') && trimmed.length >= 20 && trimmed.length <= 256) {
    // Looks like a hex/base64 token
    if (/^[A-Za-z0-9+/=_-]{20,}$/.test(trimmed)) return true
  }
  // Common secret patterns
  const secretPatterns = [
    /^(AKIA|ASIA)[A-Z0-9]{16}$/,           // AWS access key
    /^sk-[a-zA-Z0-9]{20,}$/,               // OpenAI API key
    /^ghp_[a-zA-Z0-9]{36}$/,               // GitHub PAT
    /^xox[bpsa]-[a-zA-Z0-9-]+$/,           // Slack token
    /^Bearer\s+[A-Za-z0-9._~+/=-]+$/i,     // Bearer token
    /password[\s:=]+\S+/i,                  // password=xxx
    /secret[\s:=]+\S+/i,                    // secret=xxx
    /^-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY/, // PEM private key
  ]
  return secretPatterns.some(p => p.test(trimmed))
}

export { MAX_CLIPBOARD_CHARS }

// ── Notification summary truncation (Property 9) ─────────────────────────

const MAX_SUMMARY_CHARS = 100

/**
 * Truncate notification summary to MAX_SUMMARY_CHARS.
 * Counts each character (including CJK) as 1.
 * Property 9: result length never exceeds 100 characters.
 */
export function truncateSummary(summary: string): string {
  if ([...summary].length <= MAX_SUMMARY_CHARS) return summary
  return [...summary].slice(0, MAX_SUMMARY_CHARS).join('')
}

export { MAX_SUMMARY_CHARS }

// ── Screen bounds validation (Property 4) ────────────────────────────────

export interface ScreenBounds {
  width: number
  height: number
  x?: number   // origin x (for multi-monitor, can be negative)
  y?: number   // origin y (for multi-monitor, can be negative)
}

export interface CaptureRegion {
  x: number
  y: number
  width: number
  height: number
}

/**
 * Compute the union bounding rectangle of all displays.
 * On single-monitor setups this is just { x:0, y:0, width, height }.
 * On multi-monitor setups the union covers all screens including negative coords.
 */
export function unionBounds(screens: ScreenBounds[]): Required<ScreenBounds> {
  if (screens.length === 0) return { x: 0, y: 0, width: 0, height: 0 }
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const s of screens) {
    const ox = s.x ?? 0
    const oy = s.y ?? 0
    minX = Math.min(minX, ox)
    minY = Math.min(minY, oy)
    maxX = Math.max(maxX, ox + s.width)
    maxY = Math.max(maxY, oy + s.height)
  }
  return { x: minX, y: minY, width: maxX - minX, height: maxY - minY }
}

/**
 * Validate that a capture region is within the union of all screen bounds.
 * Supports multi-monitor setups where coordinates can be negative.
 * Property 4: out-of-bounds regions must be rejected, not silently clipped.
 */
export function validateCaptureRegion(
  region: CaptureRegion,
  screens: ScreenBounds | ScreenBounds[]
): { valid: true } | { valid: false; reason: string } {
  const screenList = Array.isArray(screens) ? screens : [screens]
  const bounds = unionBounds(screenList)

  if (region.width <= 0 || region.height <= 0) {
    return { valid: false, reason: `Non-positive dimensions: ${region.width}×${region.height}` }
  }
  if (region.x < bounds.x) {
    return { valid: false, reason: `x=${region.x} is left of leftmost screen edge (${bounds.x})` }
  }
  if (region.y < bounds.y) {
    return { valid: false, reason: `y=${region.y} is above topmost screen edge (${bounds.y})` }
  }
  if (region.x + region.width > bounds.x + bounds.width) {
    return {
      valid: false,
      reason: `Region right edge ${region.x + region.width} exceeds union width ${bounds.x + bounds.width}`,
    }
  }
  if (region.y + region.height > bounds.y + bounds.height) {
    return {
      valid: false,
      reason: `Region bottom edge ${region.y + region.height} exceeds union height ${bounds.y + bounds.height}`,
    }
  }
  return { valid: true }
}
