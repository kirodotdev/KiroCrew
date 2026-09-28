"""The committed query/fragment set the episodic admission gate is measured on.

Data only — :mod:`.admission` carries the measurement. Kept in a separate module
because the numbers are only as trustworthy as the labels, and a reviewer has to
be able to read all fifty topics without scrolling past scoring code.

**What "relevant" means here.** Each topic pairs one question with two fragments
that both carry the fact the question asks for: one under
``_EPISODIC_LONG_TEXT_CHARS`` and one above it, so the gate's length-relaxed
branch is measured on the same facts as its strict branch rather than on a
separate, easier set. A fragment is labelled relevant to its OWN topic's query
and irrelevant to the other forty-nine.

**What "irrelevant" therefore means, and why it is not a strawman.** A distractor
is another fragment from the same person's own working life — a different port, a
different retention window, a different tooling decision. That is the population
the admission gate actually faces: ``search_episodic`` scores a query against
every embedded row the store holds, and a real store holds one user's technical
conversations, not a mixture of one relevant memory and forty-nine sentences from
an unrelated domain. Sampling distractors from an unrelated domain would report a
separation the gate never has to achieve in production.

**Shape constraints the fragments must satisfy**, because ``write_episodic``
enforces them and a silently-dropped fragment would shrink the sample without
shrinking the reported denominator: 10–2,000 characters; no
``_INJECTION_PATTERNS`` match; and a distinct lowercased first-80-character
prefix per fragment, since the store's text-hash dedup rejects a second row
sharing one. :func:`kiro_crew.eval.bench.admission.validate_corpus` checks all
four and refuses to measure rather than reporting a thinned set.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AdmissionTopic:
    """One question and two same-fact fragments, one short and one long.

    ``short`` and ``long`` state the same fact at two lengths. That is what makes
    the long-text relaxation measurable: the only variable between the two rows
    is length, so a cosine difference between them is the dilution the relaxed
    threshold exists to compensate for, rather than a difference in how well the
    fragment answers the question.
    """

    topic_id: str
    query: str
    short: str
    long: str


ADMISSION_TOPICS: tuple[AdmissionTopic, ...] = (
    AdmissionTopic(
        topic_id="postgres-port",
        query="which port is my local Postgres instance listening on?",
        short=(
            "Moved the local Postgres instance to port 5433 — Docker Desktop already had "
            "5432 bound and the two kept fighting over it on every reboot."
        ),
        long=(
            "Spent most of Tuesday morning on a connection refused error that turned out to "
            "be a port collision: Docker Desktop grabs 5432 for its own Postgres container at "
            "login, so the Homebrew instance never got the socket. Rebound the Homebrew one to "
            "5433 and updated the DATABASE_URL in the local env file to match. Both can now run "
            "at once, which is what I wanted for testing the migration against two versions."
        ),
    ),
    AdmissionTopic(
        topic_id="ci-shard-timeout",
        query="how long before a CI shard gets killed?",
        short=(
            "Our CI shards are capped at 45 minutes. Anything slower gets killed with no log "
            "tail, which is why the browser suite looked like it was passing."
        ),
        long=(
            "Worth writing down because it wasted a day: the per-shard limit on the test runner "
            "is forty-five minutes, and when a shard hits it the runner terminates the process "
            "without flushing the log. So the browser suite showed a green summary line from an "
            "earlier phase and no failure anywhere, while the actual cause was that the shard "
            "never finished. Split it into two shards and both come in around twenty minutes."
        ),
    ),
    AdmissionTopic(
        topic_id="staging-db-restore",
        query="how do we refresh the staging database from production?",
        short=(
            "Staging gets refreshed from the nightly production snapshot, with the customer "
            "email column scrambled by the anonymise step before anyone can connect."
        ),
        long=(
            "The refresh path for staging data: take the previous night's production snapshot, "
            "restore it into the staging cluster, then run the anonymise job before opening the "
            "security group back up. The anonymise job rewrites customer emails and phone "
            "numbers to generated values and truncates the payment audit table entirely. Order "
            "matters — the group stays closed until the job reports success, so nobody can query "
            "real addresses through the window while the restore is still settling."
        ),
    ),
    AdmissionTopic(
        topic_id="dark-mode-preference",
        query="do I prefer the light or the dark editor theme?",
        short=(
            "I keep the editor on the dark theme all day, but switch the terminal to light when "
            "I am working outside on the balcony."
        ),
        long=(
            "A small preference that keeps coming up when I set up a new machine: editor stays "
            "on the dark theme permanently, because I have three windows open and the bright "
            "background is tiring by the afternoon. The terminal is the exception — outdoors the "
            "dark background reflects too much and I switch that one to light. Screen sharing is "
            "the other exception, since the dark theme washes out badly through compression."
        ),
    ),
    AdmissionTopic(
        topic_id="slack-notification-hours",
        query="when should the assistant stop sending me Slack messages?",
        short=(
            "Hold my Slack notifications after 19:00 local and over the weekend unless a "
            "production alarm is actually firing."
        ),
        long=(
            "My rule for out-of-hours messages, since I keep having to restate it: nothing after "
            "seven in the evening local time, and nothing at all on Saturday or Sunday, with one "
            "exception for a production alarm that is currently firing rather than one that has "
            "already recovered. A digest the next working morning is fine for everything else, "
            "including build failures on my own branches, which can always wait."
        ),
    ),
    AdmissionTopic(
        topic_id="flaky-test-cause",
        query="what was making the checkout test flake?",
        short=(
            "The checkout test flaked because two cases shared one fixture directory and "
            "whichever finished first deleted it from under the other."
        ),
        long=(
            "Root cause of the checkout flake, finally: two test cases were resolving the same "
            "fixture directory from a module-level constant instead of taking a per-test "
            "temporary one. Under the parallel runner they land on different workers, so "
            "whichever finished first ran its cleanup and removed the directory the other was "
            "still reading from. It only reproduced under parallelism, which is why running the "
            "file on its own always looked fine and everyone assumed a timing problem."
        ),
    ),
    AdmissionTopic(
        topic_id="wheel-build-error",
        query="why did the published wheel fail to import the native library?",
        short=(
            "The wheel shipped broken because a global exclude on shared objects stripped the "
            "one library whose filename had no version suffix."
        ),
        long=(
            "The packaging bug that shipped a wheel nobody could use: the manifest carried a "
            "blanket exclude for shared object files, and every vendored library except one has "
            "a version suffix on its filename, so that single unsuffixed file was the only thing "
            "the rule caught. The wheel is built from the source archive, so the exclude applied "
            "to both, and the import fell back to a keyword path silently instead of raising. "
            "The fix was an explicit list of required files rather than a glob over what exists."
        ),
    ),
    AdmissionTopic(
        topic_id="arm-runner-cost",
        query="what did moving CI to ARM runners save us?",
        short=(
            "Switching the build jobs to ARM runners cut the monthly CI bill by roughly a third "
            "and shaved four minutes off the average run."
        ),
        long=(
            "Numbers from the runner migration, so I stop guessing at them in planning: moving "
            "the compile and test jobs onto ARM instances brought the monthly bill down about "
            "thirty per cent, and the average wall time fell from nineteen minutes to fifteen. "
            "Two jobs stayed on the old architecture because a dependency publishes no ARM "
            "build, and those are now the slowest thing in the graph. Nothing needed source "
            "changes beyond pinning that one dependency to a version that builds from source."
        ),
    ),
    AdmissionTopic(
        topic_id="redis-eviction-policy",
        query="which eviction policy is the cache cluster set to?",
        short=(
            "The cache cluster runs an approximated least-recently-used eviction policy over "
            "keys that carry a TTL, not over the whole keyspace."
        ),
        long=(
            "Documenting the cache configuration because the default surprised someone again: "
            "eviction is the approximated least-recently-used strategy restricted to keys that "
            "have a TTL set. Keys written without one are never evicted, so a code path that "
            "forgets the expiry argument leaks memory until the node refuses writes. We found "
            "two such paths during the last incident. The sampling width is five keys per pass, "
            "which is the default and is close enough to true recency at our key counts."
        ),
    ),
    AdmissionTopic(
        topic_id="jwt-clock-skew",
        query="how much clock skew do we allow on token validation?",
        short=(
            "Token validation allows sixty seconds of clock skew in either direction, which is "
            "what stopped the intermittent rejections on the mobile client."
        ),
        long=(
            "The intermittent authentication failures on the mobile client came down to clock "
            "skew: phones with a slightly fast clock were presenting tokens whose issued-at time "
            "was in the server's future, and the library rejected them outright. We now allow "
            "sixty seconds of tolerance in both directions on issued-at and expiry alike. Wider "
            "than that starts to matter for replay, and sixty seconds covered every real device "
            "in the sample we pulled from the logs."
        ),
    ),
    AdmissionTopic(
        topic_id="onboarding-doc-location",
        query="where does the team keep its onboarding guide?",
        short=(
            "The onboarding guide lives in the platform repo under docs/onboarding, not in the "
            "wiki — the wiki copy is three years stale."
        ),
        long=(
            "Pointing new joiners at the right document, since there are two: the live one is in "
            "the platform repository under the docs directory, reviewed with every change to the "
            "setup script, and the wiki page with the same title has not been touched in three "
            "years and still describes the old build system. I asked for the wiki page to be "
            "replaced with a link rather than deleted, so search results lead somewhere useful."
        ),
    ),
    AdmissionTopic(
        topic_id="release-cadence",
        query="how often do we cut a release?",
        short=(
            "We cut a release candidate every second Wednesday and promote it to stable the "
            "following Monday if nothing regresses over the weekend."
        ),
        long=(
            "Our release rhythm, written down for planning: a candidate is cut on alternate "
            "Wednesdays, sits for four days while the insiders channel exercises it, and is "
            "promoted to stable on the Monday if nothing has regressed. Promotion never "
            "rebuilds, so anything a stable user will read has to be baked into the candidate "
            "before it is cut. A hotfix can go out on any day, but it branches from the last "
            "stable tag rather than from the mainline."
        ),
    ),
    AdmissionTopic(
        topic_id="coffee-order",
        query="what is my usual coffee order?",
        short=(
            "My usual is a double espresso with a small glass of cold water on the side, and no "
            "sugar. Second one only before noon."
        ),
        long=(
            "For anyone ordering on my behalf: a double espresso, no sugar, with a small glass of "
            "cold water alongside. If the place only does long drinks then a flat white is a fine "
            "substitute, but not with oat milk, which I do not get on with. I stop after the "
            "second cup and never have one after midday, because it costs me the first hour of "
            "sleep and I notice it the whole next day."
        ),
    ),
    AdmissionTopic(
        topic_id="keyboard-layout",
        query="which keyboard layout do I use?",
        short=(
            "I type on a Colemak layout with the operating system remapping it, so a fresh "
            "machine feels wrong until that is set."
        ),
        long=(
            "Setting up a new machine always trips over this: I use the Colemak layout, applied "
            "at the operating system level rather than in firmware, because I share the keyboard "
            "with someone who uses the standard layout and the toggle needs to be reachable. The "
            "consequence is that any tool reading raw key positions rather than characters sees "
            "the wrong keys, which is why the editor shortcuts need their own remapping pass."
        ),
    ),
    AdmissionTopic(
        topic_id="commute-preference",
        query="how do I get to the office?",
        short=(
            "I cycle to the office when it is dry, about twenty-five minutes, and take the tram "
            "when it is raining or I am carrying the monitor."
        ),
        long=(
            "My commute, for scheduling purposes: normally the bicycle, which is twenty-five "
            "minutes door to door and slightly faster than the tram at rush hour. In the rain, or "
            "on a day I have to carry the second monitor, I take the tram instead and that is "
            "closer to forty minutes with the walk at each end. Either way I would rather not "
            "have a meeting before half past nine, since both options put me in around nine."
        ),
    ),
    AdmissionTopic(
        topic_id="terraform-state-lock",
        query="what do I do about a stuck infrastructure state lock?",
        short=(
            "A stuck state lock is almost always a cancelled pipeline run; check the lock table "
            "for the run id before force-unlocking anything."
        ),
        long=(
            "Procedure for a stuck state lock, because force-unlocking blind has bitten us: the "
            "lock row records the identity and the run that took it, so look that up first. Nine "
            "times out of ten it is a pipeline run somebody cancelled in the browser, which "
            "leaves the lock behind, and unlocking is safe. The tenth is a run still in progress "
            "on a runner, and unlocking then gives you two concurrent applies against one state "
            "file. If the run is live, wait for it, however long that takes."
        ),
    ),
    AdmissionTopic(
        topic_id="dns-propagation-wait",
        query="how long do we wait for a DNS change to take effect?",
        short=(
            "Our records carry a 300 second TTL, so a DNS change is fully live within five "
            "minutes — no reason to wait the old hour."
        ),
        long=(
            "Clearing up a habit from the old setup: every record in the zone now carries a five "
            "minute time to live, so a change is everywhere within five minutes and the hour-long "
            "wait people still schedule is cargo cult. The exception is the delegation records at "
            "the registrar, which are cached for two days and cannot be shortened, so a "
            "nameserver change genuinely does need planning. Ordinary address record swaps do not."
        ),
    ),
    AdmissionTopic(
        topic_id="s3-lifecycle-rule",
        query="when do the raw upload objects get deleted?",
        short=(
            "Raw uploads move to cold storage after thirty days and are deleted at ninety, which "
            "is why the reprocessing job only accepts recent files."
        ),
        long=(
            "The lifecycle policy on the upload bucket, since it constrains what reprocessing can "
            "reach: an object transitions to the cold tier thirty days after creation and expires "
            "at ninety. Restoring from the cold tier takes hours, so the reprocessing job simply "
            "refuses anything older than thirty days rather than blocking on a restore nobody is "
            "watching. Derived thumbnails live in a separate prefix with no expiry, because "
            "regenerating them costs more than storing them."
        ),
    ),
    AdmissionTopic(
        topic_id="lambda-cold-start",
        query="what did we do about the function cold start latency?",
        short=(
            "Cold starts dropped from 2.4 seconds to about 400 milliseconds once we stopped "
            "importing the whole client library at module scope."
        ),
        long=(
            "The cold start work, with the numbers so I can point at them: the handler was "
            "importing the full cloud client library at module scope, which cost 2.4 seconds "
            "before the first line of our own code ran. Deferring those imports into the two "
            "functions that actually need them took the cold start to roughly four hundred "
            "milliseconds. Provisioned capacity would have cost real money to fix a problem that "
            "turned out to be one import statement in the wrong place."
        ),
    ),
    AdmissionTopic(
        topic_id="grafana-dashboard-owner",
        query="who owns the latency dashboard?",
        short=(
            "The latency dashboard is owned by the platform team; the checkout team owns the "
            "conversion one that looks similar and is often confused for it."
        ),
        long=(
            "Ownership of the two dashboards that keep getting mixed up: the request latency one, "
            "with the percentile panels, belongs to the platform team and its alerts page them. "
            "The conversion funnel dashboard has a nearly identical layout because it was copied "
            "from that one, but it belongs to the checkout team and its thresholds are business "
            "targets rather than alarms. Editing the wrong one is how we ended up paging platform "
            "for a marketing dip last quarter."
        ),
    ),
    AdmissionTopic(
        topic_id="pagerduty-escalation",
        query="how long until an unacknowledged page escalates?",
        short=(
            "An unacknowledged page escalates to the secondary after ten minutes and to the "
            "engineering manager after twenty-five."
        ),
        long=(
            "The escalation timings on the primary rota, which I had to look up during the last "
            "handover: the primary has ten minutes to acknowledge before the page moves to the "
            "secondary, and a further fifteen after that before it reaches the engineering "
            "manager. Out of hours the first step is shortened to five minutes, on the reasoning "
            "that someone asleep is less likely to acknowledge at all and the secondary is in a "
            "different time zone deliberately."
        ),
    ),
    AdmissionTopic(
        topic_id="code-owner-frontend",
        query="who reviews changes to the design system package?",
        short=(
            "Design system changes need a review from the design systems group, and their file "
            "pattern covers the whole packages directory, not just components."
        ),
        long=(
            "Review routing for the shared component work: any change under the packages "
            "directory requires an approval from the design systems group, and the pattern is "
            "deliberately wider than the component folder because the token definitions and the "
            "build configuration live alongside it. A change that only touches a story file "
            "still trips it, which is mildly annoying but has caught two token renames that would "
            "have broken downstream consumers silently."
        ),
    ),
    AdmissionTopic(
        topic_id="python-version-floor",
        query="what is the minimum Python version we support?",
        short=(
            "We support Python 3.10 and up. That floor is why the codebase imports future "
            "annotations everywhere instead of using the newer syntax directly."
        ),
        long=(
            "The interpreter floor and its consequences, since a review keeps rediscovering "
            "them: the minimum supported version is 3.10, which is what a couple of the distros "
            "our users are on still ship. That means the newer match-statement patterns are fine "
            "but the newer typing syntax is not, so modules import future annotations at the top "
            "and write type hints as strings where they are evaluated at runtime. Raising the "
            "floor is a user-visible change and belongs in a release note, not in a refactor."
        ),
    ),
    AdmissionTopic(
        topic_id="node-version-pin",
        query="which Node version does the frontend build need?",
        short=(
            "The frontend build needs Node 22; the bundler's native addon has no prebuild for 20 "
            "and compiles from source for ten minutes if you try."
        ),
        long=(
            "Pinning the runtime for the web build: version 22 is required, and the reason is not "
            "language features but a native addon in the bundler that publishes prebuilt binaries "
            "only for 22 and later. On 20 the install silently falls back to compiling it from "
            "source, which takes about ten minutes and then fails on a machine without a "
            "toolchain. The version file at the repository root is what the setup script reads, "
            "so changing it in one place is enough."
        ),
    ),
    AdmissionTopic(
        topic_id="lint-rule-disabled",
        query="why is the unused-variable lint rule turned off in the generated directory?",
        short=(
            "The unused-variable rule is off for generated code only, because the code generator "
            "emits placeholder bindings we do not control."
        ),
        long=(
            "An exception in the lint configuration that looks like laziness and is not: the "
            "unused-variable rule is disabled for the generated directory, because the schema "
            "code generator emits a placeholder binding for every optional field whether or not "
            "anything reads it. We do not control that output and regenerating is part of the "
            "build, so the alternative was a thousand suppression comments rewritten on every "
            "regeneration. The rule stays on everywhere a human writes code."
        ),
    ),
    AdmissionTopic(
        topic_id="type-checker-platform",
        query="why does the type checker report different errors on my laptop than in CI?",
        short=(
            "Pass the platform flag when type checking locally — CI checks against Linux, and the "
            "stubs guard several filesystem calls behind that platform."
        ),
        long=(
            "The local versus continuous integration discrepancy in type checking comes down to "
            "the target platform. The checker assumes the platform it is running on, the "
            "integration jobs run on Linux, and the type stubs put several extended-attribute "
            "filesystem functions behind a Linux-only guard even though they exist on macOS. So a "
            "laptop run reports errors in files nobody touched and, worse, misses the Linux-only "
            "errors that fail the job. Passing the platform flag explicitly makes both agree."
        ),
    ),
    AdmissionTopic(
        topic_id="migration-rollback-plan",
        query="can we roll back the column rename migration?",
        short=(
            "The column rename is not reversible in place — the rollback path is to restore from "
            "the pre-migration snapshot and replay the write log."
        ),
        long=(
            "Being honest about the rename migration: it is not reversible with a down step, "
            "because the old column is dropped and the data in it is transformed on the way "
            "across rather than copied. The rollback plan is therefore a restore from the "
            "snapshot taken immediately before, followed by replaying the write-ahead log up to "
            "the migration timestamp. That is a fifteen minute outage, which is why it is "
            "scheduled for a Sunday and why the expand-and-contract shape is worth the extra "
            "release next time."
        ),
    ),
    AdmissionTopic(
        topic_id="feature-flag-cleanup",
        query="what is our policy on removing old feature flags?",
        short=(
            "A feature flag gets removed within two releases of reaching full rollout; the ones "
            "we kept longer are now indistinguishable from configuration."
        ),
        long=(
            "The rule we agreed on for flags, after finding one that had been at full rollout for "
            "fourteen months: a flag is deleted within two releases of reaching every user, and "
            "the deletion is the same change that removes the off branch. Anything older than "
            "that stops being a rollout mechanism and becomes undocumented configuration, which "
            "is how we ended up with two code paths where nobody could say which one production "
            "was on. A flag intended to be permanent is a setting and belongs in the config file."
        ),
    ),
    AdmissionTopic(
        topic_id="rate-limit-header",
        query="which header tells a client how long to wait after a rate limit?",
        short=(
            "On a rate limited response we send retry-after in seconds, and clients that read "
            "only the remaining-quota header end up hammering us."
        ),
        long=(
            "How our rate limiting communicates back, because two client libraries get it wrong: "
            "a throttled response carries a retry-after header measured in seconds, and that is "
            "the value to sleep for. The remaining-quota and reset-at headers are informational "
            "and are only present on successful responses, so a client that keys its backoff off "
            "them sees nothing on the response that actually mattered and retries immediately. "
            "We now log the user agent of anything retrying inside one second."
        ),
    ),
    AdmissionTopic(
        topic_id="websocket-heartbeat",
        query="how often does the socket connection send a heartbeat?",
        short=(
            "The socket sends a ping every twenty seconds because the load balancer closes idle "
            "connections at sixty and gave no close frame."
        ),
        long=(
            "Why the socket heartbeat is twenty seconds and not something rounder: the load "
            "balancer in front of the service drops a connection with no traffic for sixty "
            "seconds, and it drops it without sending a close frame, so the client only notices "
            "on its next write. Twenty seconds gives two chances to keep it alive before that "
            "limit and is cheap at our connection count. The client treats two missed pongs as a "
            "dead connection and reconnects with jitter to avoid a thundering herd."
        ),
    ),
    AdmissionTopic(
        topic_id="image-cdn-cache",
        query="how do we invalidate a cached image at the edge?",
        short=(
            "We never invalidate images at the edge — the upload path writes a content hash into "
            "the filename so a new image is a new URL."
        ),
        long=(
            "Our approach to edge caching for images is to make invalidation unnecessary: the "
            "upload pipeline computes a content hash and puts it in the object name, so any "
            "change produces a different URL and the cached copy of the old one can simply "
            "expire. Cache lifetime is a year and marked immutable. The one place this does not "
            "hold is the site logo, which is referenced by a stable path for third-party "
            "embedding, and that one genuinely does need a purge when it changes."
        ),
    ),
    AdmissionTopic(
        topic_id="font-licensing",
        query="are we allowed to self-host the brand typeface?",
        short=(
            "The brand typeface licence covers self-hosting for our own domains but not "
            "redistribution, so it must stay out of the published npm package."
        ),
        long=(
            "The typeface licence, since this comes up whenever someone packages the design "
            "system: self-hosting the web font on domains we own is covered, and so is embedding "
            "it in our own applications. Redistribution is not, which means the font files cannot "
            "go into the package we publish to the public registry — a consumer installing it "
            "would be receiving a copy we have no right to give them. The package references the "
            "font by family name and documents where to obtain it."
        ),
    ),
    AdmissionTopic(
        topic_id="accessibility-audit",
        query="what did the accessibility audit flag as most serious?",
        short=(
            "The audit's worst finding was the custom select control: keyboard focus never "
            "entered the option list, so it was unusable without a mouse."
        ),
        long=(
            "Summary of the accessibility audit's serious findings, in the order they matter: "
            "first, the custom select control never moved keyboard focus into its option list, "
            "which made it entirely unusable without a pointer and affected every form on the "
            "site. Second, four icon-only buttons had no accessible name. Third, the error "
            "summary was announced before the fields it described were updated, so a screen "
            "reader user heard a count with no detail. The contrast findings were all in one "
            "deprecated theme."
        ),
    ),
    AdmissionTopic(
        topic_id="i18n-plural-rule",
        query="how many plural forms do we need to support?",
        short=(
            "Our catalogue needs six plural forms, because Arabic and Welsh both use categories "
            "the two-form English shape cannot express."
        ),
        long=(
            "Plural handling in the translation catalogue: we ship twelve locales and between "
            "them they need all six plural categories, which is driven by Arabic and Welsh rather "
            "than by the majority of the set. This is why every countable string goes through the "
            "plural helper even when the English has exactly two forms — hardcoding a singular "
            "and a plural produces text that is simply wrong in those locales, and it is invisible "
            "to anyone reviewing in English. The lint rule checks for the bare pattern."
        ),
    ),
    AdmissionTopic(
        topic_id="timezone-storage",
        query="how do we store timestamps in the database?",
        short=(
            "Every timestamp is stored in UTC with an explicit offset, and the user's zone is a "
            "separate column used only for rendering."
        ),
        long=(
            "The timestamp convention, which one service still violates: store the instant in "
            "UTC with an explicit offset on the column type, and keep the user's own zone in a "
            "separate profile field that is used only when rendering. Never store a local time "
            "with the zone implied by the row's owner, because the owner can move and the "
            "historical instants then shift underneath the reports. The scheduling service is the "
            "exception and stores a wall-clock time deliberately, since a nine o'clock reminder "
            "should stay at nine after a move."
        ),
    ),
    AdmissionTopic(
        topic_id="currency-rounding",
        query="how do we round money in the invoice totals?",
        short=(
            "Money is stored in minor units as integers and rounded half up at the line level, "
            "then summed — never summed and rounded once."
        ),
        long=(
            "The money rules, which an auditor asked about and I want written down: amounts are "
            "integers in minor units, so no floating point anywhere in the pricing path. Rounding "
            "happens per line item, half away from zero, and the invoice total is the sum of the "
            "already-rounded lines. Summing the unrounded lines and rounding the total produces a "
            "figure a penny different from what the customer sees itemised, which is the "
            "discrepancy that started the whole conversation."
        ),
    ),
    AdmissionTopic(
        topic_id="csv-export-encoding",
        query="why does the exported spreadsheet show broken characters?",
        short=(
            "The export writes a byte order mark before the UTF-8 content, because without it "
            "the spreadsheet application guesses the local codepage."
        ),
        long=(
            "The mangled accents in exported files were not an encoding bug on our side: the file "
            "is UTF-8, but the spreadsheet application on Windows guesses the legacy local "
            "codepage when opening a comma-separated file with no marker, and there is no way to "
            "signal the encoding in the format itself. Writing a byte order mark at the start "
            "makes it detect UTF-8 correctly. Anything parsing the file programmatically has to "
            "strip that marker, so the API download endpoint deliberately omits it."
        ),
    ),
    AdmissionTopic(
        topic_id="pdf-render-timeout",
        query="why do large reports fail to render as PDF?",
        short=(
            "The PDF renderer times out at thirty seconds, and a report over about eighty pages "
            "spends most of that laying out the tables."
        ),
        long=(
            "Large report exports fail because of a renderer timeout rather than a memory "
            "problem: the worker gives the layout engine thirty seconds, and past roughly eighty "
            "pages the table layout alone exceeds that, mostly recalculating column widths that "
            "never change. The fix we settled on was fixing the widths in the template rather "
            "than raising the timeout, which took a hundred and twenty page report from a "
            "failure to eleven seconds. The queue still caps a single job at two minutes."
        ),
    ),
    AdmissionTopic(
        topic_id="email-bounce-handling",
        query="what happens after an email to a customer bounces?",
        short=(
            "A hard bounce suppresses the address immediately; soft bounces retry for "
            "seventy-two hours before they are treated the same way."
        ),
        long=(
            "Bounce handling, because the suppression list surprised support: a hard bounce puts "
            "the address on the suppression list straight away and no further mail is attempted, "
            "which is what our sending reputation requires. A soft bounce is retried with backoff "
            "for seventy-two hours and only then suppressed. Support can clear an entry manually "
            "after confirming the address with the customer, and that action is audited, because "
            "clearing a genuine hard bounce is how a sender gets itself blocked."
        ),
    ),
    AdmissionTopic(
        topic_id="password-reset-window",
        query="how long is a password reset link valid?",
        short=(
            "A password reset link is valid for one hour and is single use — following it twice "
            "fails even inside the window."
        ),
        long=(
            "Reset link semantics, which a support ticket questioned: the token is valid for one "
            "hour from issue and is consumed on first successful use, so a second visit fails "
            "even within the hour. Requesting a new link invalidates any outstanding one, meaning "
            "a customer who clicks the older email after requesting twice sees a failure that "
            "looks like a bug. We changed the copy to say so rather than lengthening the window, "
            "since the window is the part protecting a forwarded mailbox."
        ),
    ),
    AdmissionTopic(
        topic_id="audit-log-retention",
        query="how long do we keep the audit log?",
        short=(
            "Audit records are kept for seven years in the append-only store; the application "
            "database only holds the last ninety days."
        ),
        long=(
            "Retention for audit data is split across two places and people conflate them: the "
            "application database keeps ninety days so the in-product activity view stays fast, "
            "and every record is also written to an append-only archive retained for seven years "
            "to satisfy the financial requirement. Deletion from the archive is not possible by "
            "design, which is worth knowing before anyone writes personal data into an audit "
            "message — the erasure request path cannot reach it."
        ),
    ),
    AdmissionTopic(
        topic_id="backup-verification",
        query="how do we know the backups actually restore?",
        short=(
            "A restore drill runs every Sunday against a scratch cluster and fails the pipeline "
            "if the row counts do not match the source."
        ),
        long=(
            "Backup verification is automated rather than assumed, which was the action item "
            "from the near miss last year: every Sunday a job restores the latest snapshot into "
            "a scratch cluster, runs a row count and checksum comparison against the source for "
            "the ten largest tables, and fails loudly if anything differs. The scratch cluster is "
            "destroyed afterwards. Before this existed the backups had been running green for "
            "eight months while the largest table was excluded by a filter nobody had reread."
        ),
    ),
    AdmissionTopic(
        topic_id="secret-rotation-period",
        query="how often are service credentials rotated?",
        short=(
            "Service credentials rotate every ninety days automatically; the two that still need "
            "a human are the ones a vendor issues by email."
        ),
        long=(
            "Credential rotation, and where it is still manual: anything issued by our own "
            "identity provider rotates on a ninety day schedule with an overlap window, so "
            "nothing has to be restarted. Two vendor credentials are outside that because the "
            "vendor issues them through a human process and emails the value, which means a "
            "calendar reminder and somebody pasting into the secret store. Those two are the ones "
            "most likely to expire unnoticed and they are both on the critical payment path."
        ),
    ),
    AdmissionTopic(
        topic_id="vpn-split-tunnel",
        query="does the corporate VPN route all my traffic?",
        short=(
            "The VPN is split tunnel — only the internal address ranges go through it, so a "
            "video call is unaffected while it is connected."
        ),
        long=(
            "How the corporate tunnel is configured, since people disconnect it unnecessarily: "
            "it is split tunnel, carrying only the internal address ranges and the internal "
            "resolver, so ordinary internet traffic and video calls go out over the local "
            "connection and are not affected by having it up. The exception is the resolver, "
            "which does capture all name lookups, so an internal hostname resolves and a home "
            "device on a private range sometimes does not while connected."
        ),
    ),
    AdmissionTopic(
        topic_id="laptop-disk-encryption",
        query="is full disk encryption required on development machines?",
        short=(
            "Full disk encryption is mandatory on any machine that clones a repository, and the "
            "device check enforces it before issuing credentials."
        ),
        long=(
            "The device requirement for development machines: full disk encryption has to be on "
            "before the machine can obtain credentials, and the posture check verifies it at "
            "every login rather than once at enrolment. This applies to any machine that clones "
            "a repository, including a personal one, which is the part people are surprised by. "
            "A machine that fails the check keeps its existing session until it expires and then "
            "cannot renew, so the failure surfaces as a login loop rather than an obvious message."
        ),
    ),
    AdmissionTopic(
        topic_id="mobile-crash-symbols",
        query="why are the mobile crash reports unreadable?",
        short=(
            "Mobile crash traces are unreadable when the build pipeline skips the symbol upload "
            "step, which it does silently on a retried job."
        ),
        long=(
            "Unsymbolised crash reports have one cause every time: the debug symbol bundle was "
            "not uploaded for that build. The upload is a separate pipeline step and, on a "
            "retried job, the step is skipped because its artefact already exists — but the "
            "artefact from the first attempt belongs to a different binary. So the reports come "
            "back as raw addresses. Making the step idempotent on the build identifier rather "
            "than on artefact presence fixed it, and old builds can be symbolised after the fact."
        ),
    ),
    AdmissionTopic(
        topic_id="app-store-review-time",
        query="how long does store review usually take for our app?",
        short=(
            "Store review has been running about twenty-four hours for us, but a build that "
            "changes the permission strings takes closer to four days."
        ),
        long=(
            "Planning numbers for store submissions, from the last eight releases: an ordinary "
            "build clears review in about a day, sometimes the same afternoon. A build that "
            "changes any of the permission usage descriptions goes to a human reviewer and has "
            "taken between three and five days each time, which has caught us out twice on a "
            "coordinated launch. The expedited request is worth using but is only granted for a "
            "genuine fix, so it cannot be part of the normal plan."
        ),
    ),
    AdmissionTopic(
        topic_id="analytics-sampling",
        query="is the analytics data sampled?",
        short=(
            "Analytics events are sampled at ten per cent for page views but every conversion "
            "event is sent, so the ratios between them are wrong."
        ),
        long=(
            "An important caveat about the analytics numbers: page view events are sampled at ten "
            "per cent client side to keep the volume affordable, while conversion and error "
            "events are always sent. That means any funnel calculated by dividing one by the "
            "other is off by a factor of ten unless the sampling is corrected for, and the "
            "dashboard does correct for it while ad hoc queries generally do not. This is the "
            "source of the conversion rate that looked implausibly good last month."
        ),
    ),
    AdmissionTopic(
        topic_id="ab-test-duration",
        query="how long do we run an experiment before reading it?",
        short=(
            "Experiments run a minimum of two full weeks regardless of significance, because our "
            "weekday and weekend users behave differently."
        ),
        long=(
            "The rule on experiment duration, which exists to stop people reading a result on day "
            "three: a minimum of two full weeks, covering two complete weekly cycles, whatever "
            "the significance calculation says. Our weekday and weekend populations convert "
            "differently enough that a test started on a Tuesday and read on a Friday is "
            "measuring the day of the week. The dashboard hides the result until the minimum has "
            "elapsed, which is blunt and has been worth it."
        ),
    ),
    AdmissionTopic(
        topic_id="postmortem-template",
        query="what has to be in an incident write-up?",
        short=(
            "An incident write-up needs a timeline, the customer impact in plain numbers, and "
            "action items with owners — no individual named as a cause."
        ),
        long=(
            "What we require in an incident write-up, and one thing we forbid: a timeline with "
            "timestamps from the first signal to full recovery, the customer impact stated as "
            "numbers rather than adjectives, the contributing factors, and action items that each "
            "have a named owner and a date. The forbidden part is attributing the cause to a "
            "person; if a single deploy could break it then the gap is in the guard rails, and "
            "that is the finding worth writing down. Reviewed in the weekly operations meeting."
        ),
    ),
)
