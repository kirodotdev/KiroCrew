import type { CapabilityOperation } from '../../api/crewCapabilities'

// Fixed wire sentinel. Inputs display a translated keep-value hint instead.
export const secretMask = '[REDACTED]'
export const pointerPart = (key: string) => key.replace(/~/g, '~0').replace(/\//g, '~1')
export const atOrBelow = (path: string, root: string) => path === root || path.startsWith(`${root}/`)
export function transportValue(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}
export function redactedPaths(value: unknown, path = ''): string[] {
  if (value === secretMask) return [path]
  if (!value || typeof value !== 'object') return []
  return Object.entries(value).flatMap(([key, item]) => redactedPaths(item, `${path}/${pointerPart(key)}`))
}

/** Retention belongs to the original revision, never to a freshly typed mask.
 * Once an operation exists, only its explicit retained paths may survive. */
export function retainedPaths(original: unknown, operation?: CapabilityOperation): string[] {
  return operation?.action === 'set' ? ('value' in operation ? operation.retain_paths ?? [] : []) : redactedPaths(original)
}

export function retentionValid(operation: CapabilityOperation): boolean {
  if (operation.action !== 'set' || !('value' in operation)) return true
  const masks = redactedPaths(operation.value)
  const retained = operation.retain_paths ?? []
  return masks.every(path => path !== '' && retained.includes(path)) && retained.every(path => masks.includes(path))
}

/** All unedited fields are explicitly present in the replacement. The server
 * restores only retained leaves; it does not merge omitted source fields. */
export function replaceTransportFields(
  id: string, transport: Record<string, unknown>, retain: string[],
  patch: Record<string, unknown>, replacedPaths: string[],
): CapabilityOperation {
  const retain_paths = retain.filter(path => !replacedPaths.some(root => atOrBelow(path, root)))
  return { section: 'mcpServers', id, action: 'set', value: { ...transport, ...patch }, ...(retain_paths.length ? { retain_paths } : {}) }
}
