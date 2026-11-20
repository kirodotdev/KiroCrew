/** The destination the public build ships for product usage metrics. */
export const KIRO_TELEMETRY_DESTINATION = 'kiro-telemetry'

/** Whether a destination row is the build's own Kiro telemetry, not an operator entry. */
export function isKiroTelemetryRow(row: { name: string; source?: string; locked?: boolean }): boolean {
  return row.name === KIRO_TELEMETRY_DESTINATION && row.source === 'provider' && row.locked === true
}
