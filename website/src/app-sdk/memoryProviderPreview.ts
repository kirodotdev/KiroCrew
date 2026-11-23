/**
 * Whether this Crew runs the memory provider Feature preview, as the host learned it.
 *
 * The host sets the marker once `GET /api/memory/provider-preview` answers off
 * (`components/MemoryProviderSheetHosts.tsx`). While it is set the memory verbs
 * (`requestMemoryProviderChange`, `requestMemoryMove`, `requestMemoryBench`) settle
 * `'not_enabled'` without opening anything, and the `memoryAdmin` grant line reads as
 * not enabled on this Crew.
 */

let off = false

export function setMemoryProviderPreviewOff(next: boolean): void {
  off = next
}

export function memoryProviderPreviewOff(): boolean {
  return off
}
