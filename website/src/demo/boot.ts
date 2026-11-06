/**
 * Entry point of the static demo build (`npm run build:demo`).
 *
 * Applies `?reset`, installs the in-page gateway stand-in, and only then imports
 * the real dashboard entry. The dynamic import is the ordering guarantee: every
 * module under `main.tsx` evaluates after `fetch` and `WebSocket` are replaced.
 */
import { applyResetParam } from './state'
import { installDemoTransport } from './transport'

applyResetParam()
installDemoTransport()
void import('../main')
