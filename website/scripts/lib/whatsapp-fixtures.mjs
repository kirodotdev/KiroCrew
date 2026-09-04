/**
 * Fixtures shared by the WhatsApp panel harnesses.
 *
 * The QR encodes fixed placeholder text and is NOT a pairing code. A real
 * rotating code is a credential: whoever scans it links their phone as a device
 * on the operator's account, so one must never reach a committed artifact.
 * Shared rather than copied because jscpd runs at a zero threshold.
 */
export const QR_PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAMYAAADGAQAAAACh4MLwAAABP0lEQVR42u1YyQ3DMAwT6gEyklb3SBnAgEqKTvpqgX4KPmoYufwhKJGSEvVmrfiffDw5A2vgetQobr5Hrke8W786AbYExnHmOnCfgoePJtjA0qgJYOCwccbwwobwXpF1wwYwmz0vbBceQuI2yjfpNO9tpFOt837I7S4evCHNEFDkW5G3Js2GtzimGINU4XJ4ddFCTd6E55jtJGmlBdLVLkeQNGEf3qjNrqSpmPrkW7UQQNdqFaA0+GDr+j6VbNHB9an1g+Y2N0hyyPSzwZbyNymCMTXB1nSxeZO/NTa3esqGpFNuNZk+9ZR0xa71VTYe0q2RtKmwho8WXu0HbKQ6uOGVbx3WFa8uzqrv3dNWpHo5s1nmzrc0wzY3PIo0vOZTTn8sqaUGyWyuH1fisS7YzAv6HyKdykys6sL/T9aXJ0+JwOV3UZ88HQAAAABJRU5ErkJggg=='

/** The GET /api/whatsapp/config shape, as an unpaired channel. */
export const BASE_CONFIG = {
  configured: true,
  connected: false,
  connect_error: '',
  read_only: false,
  enabled: true,
  dm_policy: 'self',
  allowed_wa_ids: [],
  groups: [],
  session_folder: '',
  state: 'unpaired',
}
