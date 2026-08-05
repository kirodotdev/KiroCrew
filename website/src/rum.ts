/**
 * Telemetry shim (no-op).
 *
 * The public KiroCrew build ships with telemetry disabled. These functions
 * preserve the original API surface (initRum, recordEvent, recordSessionStart,
 * getRum) so existing call sites keep working, but do nothing. No data is
 * collected and no external client is loaded.
 */

/**
 * Initialize telemetry. No-op in the public build.
 * Safe to call multiple times and with any argument.
 */
export function initRum(_appVersion: string): void {
  // Telemetry is disabled in the public build.
}

/**
 * Record a custom event. No-op in the public build.
 *
 * Usage (retained for compatibility):
 *   recordEvent('page_view', { page: '/chat' })
 *   recordEvent('feature_used', { feature: 'cron_add' })
 */
export function recordEvent(_type: string, _data: Record<string, unknown>): void {
  // Telemetry is disabled in the public build.
}

/**
 * Record a one-time `session_start` event. No-op in the public build.
 * Retains the original signature so call sites do not need to change.
 */
export function recordSessionStart(_status: {
  owner_id_hash?: string; version?: string; os_type?: string;
  arch?: string; cpu_count?: number; mem_total_gb?: number; platform?: string;
}, _retries = 5): void {
  // Telemetry is disabled in the public build.
}

/**
 * Expose the raw telemetry client. Always null in the public build.
 */
export function getRum(): null {
  return null
}
