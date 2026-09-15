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

## Related docs

- [Dashboard](dashboard.md): the side panel and where its tabs live
- [Computer use](computer-use.md): driving native desktop apps rather than a web page
- [Artifacts](artifacts.md): keeping a screenshot or a page the agent produced
