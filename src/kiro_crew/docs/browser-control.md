# Browser control

The agent browses with one browser, and you watch it **in the chat**. Ask it to
open a site, click through a flow, fill a form, or take a screenshot of what it
found: the turn shows a live view of the page while it works, and collapses to a
one-line record (a thumbnail, the step count, Replay) when it is done.

Click the live view at any point to **take over** with your real mouse and
keyboard; the same card grows and says "You are in control". Press Esc, or
"Hand back", to give it back. That is how a sign-in, a CAPTCHA or a two-factor
prompt gets handled: the agent stops, you do the step, it carries on. While you
hold the browser, the agent waits.

As you hand back, a bar under the page lets you **tell the agent what you did**:
pick one of a few answers (the agent's own, such as "I signed in", or Done,
Didn't work, Wrong site, Stop) and add a line of your own, then press Enter. The
agent reads it with its next step, so it can act on it straight away instead of
taking another screenshot or asking you. Esc still hands back with nothing said.
Nothing typed in that bar goes to the page. If the agent's turn is already over
(you opened the view again from the finished card), your note is sent as your
next chat message.

## Which browser

| Where Kiro Crew runs | Browser the agent drives | First run |
|---|---|---|
| A desktop with Chrome | your own Chrome, with your logins | enable **Allow remote debugging** at `chrome://inspect/#remote-debugging` once, then click **Allow** when Chrome asks |
| Windows without Chrome | Edge | same |
| A server, no display, or a remote gateway | Kiro's own Chromium, headless | none |
| Chrome's remote-debugging policy is off | Kiro's own Chromium | none |

The agent acts only on tabs it opened; it never touches the ones you have open.
Kiro's own Chromium keeps its own profile (so a login you did during a takeover
stays signed in next time), and the agent cannot read that profile's files.

## Signed in before the first page

Kiro's own Chromium can start already signed in, so a headless or remote browser
reaches a site behind your sign-in without a takeover. Your organisation's edition
does this if it provides sign-in for its own sites and
`browser.credentials.enabled` is on, for the domains in
`browser.credentials.domains` (an edition may turn both on by default). The gateway
applies it before the agent's first step and again whenever the browser restarts.
When you sign in again with your organisation's tool, or change either setting, the
next page the agent opens picks it up. These cookies last only until the browser
closes.

Your own Chrome is never filled this way: it already has your logins, and the
gateway does not write into your profile. The agent never sees a cookie value: not
in a tool result, a page outline, a log or the audit record. The sign-in reaches
every agent browsing on this gateway, including the members and temporary sessions
it serves.

## What the agent can do to a page

Navigate to a URL, go back, read the page as a structured outline of its
elements, click (by element or by position, single or double), drag, scroll,
type, press a key, hover, choose from a dropdown, wait for text or for the page
to load, read the browser console, take a screenshot (saved as a PNG it can show
you in the chat), and list or switch its own tabs.

It **never types a password**. A password field is refused, and the live view
masks one, so a sign-in is always your step through takeover.

Reading a page it can already reach is cheaper than driving it, so a request that
only needs the text of a public page may be answered without the browser at all.
Driving it is for the cases that need interaction, a logged-in session, or a page
whose content only exists once its scripts have run.

## Local addresses are refused

The agent cannot navigate to `localhost`, a loopback address, or a literal
private-network or link-local IP address. Those are where your own control
planes live, this dashboard among them, so driving one would let the agent reach
an interface it is not supposed to operate. Only public HTTP(S) targets open,
plus the exact `host:port` origins you list under **Settings → Browser → Local
preview origins** (a dev server you want it to check, say). The dashboard's own
port is refused even when listed.

The gate does not resolve DNS names. A hostname that later resolves to a private
address can therefore pass this check, so a successful navigation is not proof
that the destination is public.

## The live view

Frames stream only to your own dashboard session, over the connection it already
has, at up to 60 frames per second on a fast link. The stream adapts to the
link: on a slow one it drops the frame rate and quality first, then the size,
and climbs back when the link recovers. A page that is not changing costs
nothing, and so does a card that has scrolled off screen.

**Watch it big.** Expand (in the live card's menu) moves the view into the side
panel's **Preview** tab, with a Full screen button; on a phone it opens full
screen. The chat card folds to one line with **Bring back to chat**.

**Replay.** Each browsing turn is recorded so you can play it back: Replay on the
finished card opens the same big view at the start, with a timeline of what was
clicked, typed, scrolled and opened, and when. Click a marker or a step to jump
there, step forward and back, play at 1x, 2x or 4x, and go back to Live while
the turn is still running. Clicks, drags, scrolls and typing are drawn over the
picture where they happened. Recordings stay on this machine, readable only from
your own dashboard, under the data home in `browser-recordings/` (folders 0700,
files 0600). Password fields are painted over in every recorded frame, and a
frame whose password fields could not be located is not kept at all; what you
type during a takeover is kept only as a character count. Deleting a chat
deletes its recordings. `browser.recording.enabled`, `max_session_mb` and
`max_total_mb` turn recording off or cap its size.

**Fast mode.** When browser help is on (Settings → Decisions), the agent can run a
quick loop of many small moves (a game, a long form). The live view then shows
"Fast mode: N moves · rate", a bar for how much of its step or time budget it has
used, and, when it stops, why. Take over stops it at the next move. Its many
steps fold to the last few in the step list.

**Settings → Browser → Live view in chat** turns the view off, caps the frame
rate, picks a quality, and controls the password mask (on by default).

## Preview tab

The side panel keeps one browser surface of its own: the **Preview** tab, for a
local dev server the agent started for you. It opens only the origins listed
under **Local preview origins**, and the agent does not drive it; an outside URL
there answers "Open this in your Chrome, or ask Kiro". Once the chat has browsed,
the tab has two modes, **Agent browser** (the expanded live view and replay) and
**Local preview**; switching to Local preview brings the live view back to the
chat.

## Settings → Browser

| Section | What it holds |
|---|---|
| Browser in use | which browser is connected, its state, the fallback order, Test, Disconnect |
| Engine | the browser engine version; Kiro's Chromium version, Redownload |
| Privacy | no usage data leaves the machine and no cloud browser is used (not configurable) |
| Live view in chat | show the live view, quality, maximum frame rate, mask password fields |
| Local preview | the Preview tab's origins |
| Advanced | pick a browser by hand: automatic, your Chrome, Kiro's Chromium, headless |

The first visit walks through three steps (install the engine, connect your
Chrome, try it); **Skip** uses Kiro's own Chromium instead.

## Related docs

- [Dashboard](dashboard.md): the side panel and where its tabs live
- [Computer use](computer-use.md): driving native desktop apps rather than a web page
- [Artifacts](artifacts.md): keeping a screenshot or a page the agent produced
