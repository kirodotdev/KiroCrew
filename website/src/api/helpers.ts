import DOMPurify from 'dompurify'
import { fmtUnit } from '../i18n/format'

export function esc(s: string | null | undefined | number): string {
  if (s == null) return ''
  const str = String(s)
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

export function md(t: string): string {
  let h = esc(t)
  h = h.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>')
  h = h.replace(/`([^`]+)`/g, '<code>$1</code>')
  h = h.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
  h = h.replace(/\*(.+?)\*/g, '<em>$1</em>')
  return DOMPurify.sanitize(h)
}

/** Sanitize pre-escaped HTML (e.g. tool output passed through esc()). */
export function sanitize(html: string): string {
  return DOMPurify.sanitize(html)
}

/** Download rate, as a single localized compound unit.
 *
 *  `kilobyte-per-second` and `megabyte-per-second` are both in ECMA-402's
 *  sanctioned `-per-` list, so the whole rate goes through `Intl` — ru renders
 *  `512 кБ/c` rather than a Cyrillic size with a Latin `/s` welded on.
 *
 *  The input is BINARY KiB/s: `handlers_system.py` derives `net_rx_kbs` as
 *  `(bytes / 1024**2 delta) * 1024`, so one unit is 1024 bytes. ECMA-402's
 *  `kilobyte`/`megabyte` are DECIMAL (1000 / 1000000), and there is no
 *  `kibibyte-per-second` in the sanctioned list — so the value has to be
 *  converted to bytes before it is labelled, or the displayed rate is
 *  understated by 2.4% in kB and 4.9% in MB.
 *
 *  Precision rules preserved from the original: whole kB, one decimal for MB
 *  with the trailing zero kept. */
export function fmtSpeed(kbs: number): string {
  const bytesPerSec = kbs * 1024
  return bytesPerSec >= 1_000_000
    ? fmtUnit(bytesPerSec / 1_000_000, 'megabyte-per-second', { minimumFractionDigits: 1, maximumFractionDigits: 1 })
    : fmtUnit(Math.round(bytesPerSec / 1000), 'kilobyte-per-second', { maximumFractionDigits: 0 })
}
