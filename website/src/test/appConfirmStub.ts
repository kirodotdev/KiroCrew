import { isValidElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { onTestFinished, vi } from 'vitest'
import type { ConfirmOptions } from '../components/ConfirmDialog'
import { registerAppConfirmHost } from '../lib/appConfirm'

/**
 * Registers a fake `AppConfirmHost` that answers every `askAppConfirm()` with
 * `answer`, for tests that render a caller without `App`. Unregisters when the
 * test finishes. The returned mock records each ask's options.
 */
export function stubAppConfirm(answer: boolean) {
  const ask = vi.fn((_opts: ConfirmOptions) => Promise.resolve(answer))
  const off = registerAppConfirmHost(ask)
  onTestFinished(off)
  return ask
}

/** An ask's title and body as plain text, for asserting on its wording. */
export function confirmText(opts: ConfirmOptions): string {
  const body = opts.body == null ? ''
    : typeof opts.body === 'string' ? opts.body
      : isValidElement(opts.body) ? renderToStaticMarkup(opts.body).replace(/<[^>]+>/g, ' ')
        : String(opts.body)
  return `${opts.title} ${body}`
    .replace(/&quot;/g, '"').replace(/&#x27;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&')
}
