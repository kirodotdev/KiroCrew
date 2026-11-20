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

**One sign-in per chat.** By default every chat shares the browser's sign-ins. Set
"Browser isolation" on Settings → Browser to "Isolated browser per chat"
(`browser.isolation: per_session`) and each chat browses with its own private
cookies and site storage instead, so two chats can be signed in to two accounts
of the same site at once. A chat's set starts empty (on Kiro's own Chromium it
gets the sign-in below), lives only in memory, and is thrown away with its tabs
when the chat ends; sign in through a takeover in each chat that needs it. On a
desktop each chat's tabs open in a separate browser window. Switching the setting
takes effect at the chat's next browser step, which opens a new tab; the tabs
(and window) from the other mode stay open until the chat ends.

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

A screenshot takes optional arguments: `full` for the whole scrollable page,
`ref` for one element, `region` (`[x, y, width, height]`, the same pixels a
click uses) for a box magnified up to four times so small text is readable, and
`max_dim` for the longest side in pixels (64 to 4096). Give at most one of
`full`, `ref` and `region`. With none, the visible page is saved as before. An
option that is not valid, or that conflicts with another, is skipped and named in
a warning on the result, and the screenshot is still saved. The
same checks for passwords and filled fields apply to every form.

A page can open a JavaScript dialog (an alert, a confirm, a prompt, or a
"leave this page?" question). While one is open the page's script is held, so the
agent's other steps fail with `dialog_open`, which says what the dialog asks. The
`dialog` step reports the open dialog, and with `action` set to `accept` or
`dismiss` answers it; `text` is the answer to a prompt. With no `action` the
dialog is only reported. An `action` or `text` that is not valid is skipped and
named in a warning on the result.

**What a step changed.** With `browser.report_changes` on (off by default), a
`click`, `type` or `select_option` answer also carries `changes`, so the agent
does not need a page read after each step to see the result. It lists what the
step changed in the page's own document: the new address (without its query, path parameters or
fragment) and title, a dialog that opened or closed, new alert or status text,
which fields failed validation, and the controls that appeared or went away. A
field that failed validation is named, but its message is not quoted, because a
message can repeat what was typed; take a `snapshot` to read the message. A JavaScript
dialog the step opened is reported as `js_dialog`; `{"no_change_seen": true}`
means none of those moved. Every piece of page text is redacted and then cut to
one short line; text too long to read whole (a name past 240 characters, a
message past 640, any string past 2,000) is left out rather than cut, so no
credential is split before redaction. A message left out that way still reads
as `a message too long to show`. Text is read as the page shows it: pieces of one
word in neighbouring inline elements (a token that syntax highlighting splits)
stay one word, so redaction sees it whole, and text the page does not draw
(`display: none`, `visibility: hidden` or `collapse`) is not read. Text that
holds a token once its white space is removed (a line break or a positioned
span inside it), by a rule that starts with a fixed prefix or by any token
format the credential detectors know (a Google API key among them), is left out
too, and a message left out that way reads as
`a message that could not be shown`. Nothing a field holds is read: a native field, an editable region, an
element with a text box, search box, combobox, spin button or slider role, and a
custom element whose shadow root holds a field all count as fields, and the text
of an alert, dialog or label leaves out every field inside it. An element inside
a field (a chip in an editor) is left out with its text and attributes. A name
built from the elements `aria-labelledby` lists is read whole or not at all: a
list of more than four, or one with a piece inside a field or too long, gives no
name. A field
is named by its label, never by its value, and a password or other secret field
is left out. Content inside frames is not compared, and a page with
more than 600 elements has its controls left uncompared. The same goes for the
dialogs, messages and invalid fields when the page shows more than ten of one
kind: that list is reported as not compared, never as unchanged. Each step waits a third
of a second for the page to react, and the reads around it are bounded, so a slow
page costs the report and never the step: the step's own result stands, with a
`warning`.

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

**Stopping a task that runs on.** `browser.run.max_ops` (off by default) caps the
browser steps one chat runs; past it the next step is not run and the agent is told to
stop and say where it got to. `browser.run.max_repeats` (also off) caps how many times
in a row the same step runs with the same details (the same click on the same element,
the same scroll distance). Past it the next step is not run: the agent gets an
`Error (run_budget)` answer and tries something else. Any different step, typing
different text into one field, taking over the browser or starting a new chat counts
again from zero. A step held while you hold the browser is not counted. Set them with
`kirocrew config set browser.run.max_ops 100` or `browser.run.max_repeats 5`.

**Fast mode.** When browser help is on (the **Browser steps: pick and check** row
on the Decisions card, under Settings → Developer → Feature Previews), the agent
can run a quick loop of many small moves (a game, a long form). The live view
then shows "Fast mode: N moves · rate", a bar for how much of its step or time
budget it has used, and, when it stops, why. Take over stops it at the next
move. Its many steps fold to the last few in the step list.

**Settings → Browser → Show the live view in chat** turns the view off; under
**More**, **Quality** and **Max frame rate** set the picture. Password boxes are
always masked in the live view: `browser.live.mask_passwords` has no control on
the page and stays on unless set with `kirocrew config set`.

## Preview tab

The side panel keeps one browser surface of its own: the **Preview** tab, for a
local dev server the agent started for you. It opens only the origins listed
under **Local addresses the Preview tab can open** (Settings → Browser → More), and the agent does not drive it; an outside URL
there answers "Open this in your Chrome, or ask Kiro". That list is also the only
way the agent's `browser` tool may open a loopback address, so an origin listed for
the tab is open to the agent's browser too. Once the chat has browsed,
the tab has two modes, **Agent browser** (the expanded live view and replay) and
**Local preview**; switching to Local preview brings the live view back to the
chat.

## Settings → Browser

| Part | What it holds |
|---|---|
| Status card | which browser is connected and its state, Test, Disconnect |
| Main rows | **Which browser** (Automatic, Your Chrome, Kiro's Chromium), **Browser isolation** (shared, or isolated per chat), **Show the live view in chat**, and **Use <provider> sign-in** when your edition has a sign-in provider |
| **More** (collapsed) | **Quality**, **Max frame rate**, **Local addresses the Preview tab can open**, **Sign-in sites** (with a sign-in provider), **Engine** (versions, Download or Redownload), **Run with no window** |
| Footer | nothing is uploaded; usage data and the cloud browser are off (not configurable) |

The first visit walks through three steps (install the engine, connect your
Chrome, try it); **Skip** uses Kiro's own Chromium instead.

## Related docs

- [Dashboard](dashboard.md): the side panel and where its tabs live
- [Computer use](computer-use.md): driving native desktop apps rather than a web page
- [Artifacts](artifacts.md): keeping a screenshot or a page the agent produced
