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
literal private-network or link-local IP address. Those are where your own control
planes live — this dashboard among them — so driving one would let the agent reach
an interface it is not supposed to operate. Only public HTTP(S) targets are
auto-driven; use the approval-gated `playwright-cli` path for a local target.

The gate does not resolve DNS names. A hostname that later resolves to a private
address can therefore pass this check, so a successful navigation is not proof
that the destination is public.

## Settings → Browser

The page has three sections, one per way the agent can get a browser. They are
separate setup paths, not a switch: downloading a browser does not select it for
a session, and nothing attaches to your own browser on its own.

- **Managed browsers on the Kiro Crew host.** Playwright downloads its own
  Chromium, Firefox and WebKit builds onto the machine the gateway runs on, which
  is not always the machine you are looking at: a Mac viewing a Fedora gateway
  downloads Linux builds onto Fedora. These builds keep their own browsing session
  and do not share your personal browser's logins. Chromium is the default for
  automation. It is not Google Chrome, and WebKit is not your Safari. The page
  installs the `playwright-cli` command first, then lists each browser as
  Downloaded, offers a Download button when it is missing, and says Unknown when it
  cannot read the cache. Downloaded means the files are on disk, not that a launch
  was tested.
- **Connect your existing browser.** Attach mode drives a browser you already run,
  with your live logins and your open tabs. It needs the Playwright extension,
  which only you can install — the page links it — in a browser on the same
  machine as the gateway. Installing it in the browser on your laptop does not let
  a gateway on another machine reach that browser. An optional token stored here
  removes the per-attach approval prompt. Neither the extension nor the token
  means a browser is connected.
- **Built-in browser in the desktop app.** On by default in the desktop app, and
  only settable there. In a plain browser or on a remote gateway it reads off and
  is not editable, and the agent browses through `playwright-cli` instead.

Only one download runs at a time. While one runs, the page names it, shows its
current step and how long it has taken, and says why the other download buttons
are unavailable. Every tab and a refreshed page show the same progress, because
the gateway keeps it rather than the page. A failed download shows the step that
failed, and on Linux the command that installs any missing system libraries. On
Debian and Ubuntu the download first asks Playwright to install those libraries
itself (`--with-deps`), and retries without that step when the host refuses it.

Treat an attached browser as borrowed. The agent should not navigate a tab away
from what you were doing, and closing it would take your own windows with it.

## Related docs

- [Dashboard](dashboard.md): the side panel and where its tabs live
- [Computer use](computer-use.md): driving native desktop apps rather than a web page
- [Artifacts](artifacts.md): keeping a screenshot or a page the agent produced
