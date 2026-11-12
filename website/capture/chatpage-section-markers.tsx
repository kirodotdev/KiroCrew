/**
 * Evidence capture for section markers inside the REAL ChatPage transcript.
 *
 * Mounts ChatPage with a preloaded store and a fixture that has two marker rows
 * between turns, so a frame shows the rule where a user meets it: in the chat,
 * resolved through the renderer registry, not mounted by hand. The capture script
 * answers the page's API calls with the same fixture.
 *
 * Query params: ?theme=dark|light
 */
import { initI18n } from '../src/i18n'
import { mountChatPage } from './chatpage-mount'
import { markerFixture, type Msg } from './chatpage-section-markers-fixture'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme)

const style = document.createElement('style')
style.textContent =
  '*, *::before, *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; }'
document.head.appendChild(style)

const messages: Msg[] = markerFixture()
;(window as unknown as { __CAPTURE_MESSAGES__: Msg[] }).__CAPTURE_MESSAGES__ = messages

mountChatPage({ messages, title: 'Review queue' })
