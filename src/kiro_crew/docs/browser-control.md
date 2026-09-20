# Browser control

The dashboard has a **Browser** panel, and the agent can drive the page shown in
it. Ask it to open a site, click through a flow, fill a form, or take a screenshot
of what it found, and you watch the whole thing happen in that panel.

You can take over at any point with your real mouse and keyboard. That is how a
CAPTCHA or a two-factor prompt gets handled: the agent stops, you do the step, it
carries on.

## What the agent can do to a page

Navigate to a URL, read the page as a structured list of its elements, click,
type, press a key, hover, choose from a dropdown, take a screenshot, wait for
something to appear, go back, and read the browser console.

Reading a page it can already reach is cheaper than driving it, so a request that
only needs the text of a public page may be answered without the browser at all.
Driving it is for the cases that need interaction, a logged-in session, or a page
whose content only exists once its scripts have run.

## Local addresses are refused

The agent cannot navigate the panel to `localhost`, a loopback address, or a
private-network address. Those are where your own control planes live — this
dashboard among them — so driving one would let the agent reach an interface it is
not supposed to operate. Only globally routable addresses are accepted.

## Settings → Browser

Two things live there:

- **Install the browser engine.** Driving a page needs a browser binary. The panel
  installs it for you and shows whether it is present.
- **Attach to your own browser.** Attach mode drives your everyday browser, with
  your live logins and your open tabs, instead of a fresh one. It needs a browser
  extension that only you can install — the panel links it — and an optional token
  stored here removes the per-attach approval prompt.

Treat an attached browser as borrowed. The agent should not navigate a tab away
from what you were doing, and closing it would take your own windows with it.

## Import cookies

The Live view has an **Import cookies** button. You export the cookies from the
browser where you are already logged in — a Playwright storageState file, a
Cookie-Editor / "Get cookies.txt" export, or a Netscape `cookies.txt` — and paste
or upload them. The gateway stores them and applies them to the agent's own
browser sessions, so a fresh session starts already logged in. This is how a
logged-in session reaches a gateway running on a different machine from your
laptop.

The cookie values stay on the gateway host and are never shown back to you, and
the agent never reads the stored file — it only gets the cookies through the
browser sessions the gateway starts for it. Imported cookies apply to **new**
browser sessions automatically; a session already open picks them up on a
best-effort basis, and if it does not, the next one will. Clear them at any time
from the same control.

## Related docs

- [Dashboard](dashboard.md): the side panel and where its tabs live
- [Computer use](computer-use.md): driving native desktop apps rather than a web page
- [Artifacts](artifacts.md): keeping a screenshot or a page the agent produced
