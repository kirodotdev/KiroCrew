# Tenets

These are the principles we design and build Kiro Crew by. They are ordered. When two pull against each other, the earlier one wins and the trade-off gets written down.

1. **Safety first.** Every action is gated, auditable, and reversible where it can be, and where it cannot be you are asked first. Security is the foundation, not a feature.

2. **Build in the open.** Every feature goes in the public project, and what we run is what you run. Where a feature lands is decided in public with the reasoning written down, even when the answer is obvious, so nobody has to guess why something was or was not included.

3. **Easy to use.** Productive in 60 seconds. No configuration required to start, nothing to read first, and no AI expertise. Low floor, high ceiling, smooth path between them. The same goes for working on it: if you can run it, you should be able to change it.

4. **The gateway, not the replacement.** Kiro Crew connects the tools you already use rather than asking you to abandon them. It routes, it remembers across them, and it makes each one more useful than it is alone.

5. **Built as a community.** No two people work the same way, and that is a feature rather than something to normalize away. Skills, agents, and apps exist so anyone can shape Kiro Crew around how they already work, and what one person builds becomes something everyone can use.

6. **Knowledge that flows, with boundaries.** Memory is local-first and private by default. Sharing is explicit, controlled, and auditable. Knowledge should propagate from a person to a team to an organization to a company, but sensitive information must never spread, and the right to forget is built in.

7. **Teammates, not tools.** Agents have names, roles, and memory. They collaborate the way colleagues do, delegating work, learning preferences, and composing into teams.

8. **Everything is an app.** When we cannot build a surface as an app, that is a defect in the platform, and it is not a licence to build the surface into the core instead. What stays in the core is the trust boundary and the state every app shares — sessions and transcripts, memory, approvals, the governance ceiling, the event bus — because their worth comes from the platform holding the last word. Above that line, anything that renders or interprets is an app, and it is replaced a whole surface at a time. The set we ship is a curated opinion about where to start, open to being swapped out entirely, and it does not define the product. An app gets the same powers a built-in page has, because otherwise "make it an app" is only a polite way to say no. [`docs/architecture/overview.md`](docs/architecture/overview.md#the-app-boundary) draws the line concretely, and [`docs/request-for-change/rfc-amend-tenets-everything-is-an-app.md`](docs/request-for-change/rfc-amend-tenets-everything-is-an-app.md) records why this tenet was added and where it sits.

[GOVERNANCE.md](GOVERNANCE.md) covers who decides what lands and how. [CONTRIBUTING.md](CONTRIBUTING.md) covers how to contribute.
