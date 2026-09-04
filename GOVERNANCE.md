# Governance

This document covers who makes decisions in Kiro Crew, how those decisions get made, and how someone becomes one of the people making them. [CONTRIBUTING.md](CONTRIBUTING.md) covers how to contribute, and the two are meant to be read together.

## Who decides

**Maintainers** decide. A maintainer is someone with commit access to this repository, and the current list is in [MAINTAINERS.md](MAINTAINERS.md). Maintainers review incoming work, help contributors get a change to the point where it can land, and decide what lands. There is no separate governing body above them.

**Contributors** are everyone who sends code, documentation, apps, skills, tests, or translations, and everyone who takes part in feature discussion. Contributing does not require permission, only [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).

**Users** are everyone running Kiro Crew. Filing an issue about what is broken or missing is a real contribution to the project's direction, and most changes in direction start there.

## How decisions get made

Decisions happen in public, on the issue or pull request they belong to, with the reasoning written down. That applies to accepting a change and to declining one, because a contributor who gets a no deserves to know why.

Architectural changes get written up first as an RFC, so the design can be argued over before anyone writes the code, and so the reasoning survives after the thread scrolls away. An RFC is its own pull request adding a document to [docs/request-for-change/](docs/request-for-change/). Once it lands, the pull requests that implement it reference it by number, which leaves a trail from the finished code back to the argument that produced it.

That applies to changes to a public interface, changes other parts of the project would have to build around, and anything that would be expensive to reverse. Everything else skips it. A bug fix should never wait on a design document, and an RFC does not have to be long to count. If you are unsure which side of the line your change falls on, open an issue and ask.

Maintainers aim for agreement, and in practice most decisions are made by whichever maintainer is reviewing. Where maintainers disagree and talking it through does not resolve it, a majority of maintainers decides. There are no scheduled meetings and no formal votes to call.

The one exception to working in public is an unfixed security vulnerability, which is handled privately under [SECURITY.md](SECURITY.md) until a fix ships.

## Becoming a maintainer

Any maintainer can propose adding a new one, and the change goes ahead if the other maintainers agree. What earns the proposal is a track record: changes that landed, reviews that showed good judgment, and dependable engagement with other contributors. There is no contribution count that qualifies someone automatically.

Maintainers can step down whenever they want. A maintainer who has been unreachable for an extended stretch gets moved to the emeritus list in [MAINTAINERS.md](MAINTAINERS.md), and coming back later is just a matter of asking. Commit access can also be withdrawn for a Code of Conduct violation.

## Trademarks and amendments

**Trademarks are governed separately.** The Kiro and Kiro Crew names and logos are trademarks, and they are not licensed under this project's software license. See [NOTICE](NOTICE) for ownership. Maintainers have no authority to license, transfer, or redefine their use. The code in this repository is open source. The marks are not.

**This document changes the same way anything else does**, by a pull request that maintainers agree to. Governance is expected to grow as the project does, and a project with more maintainers than this one may well need more structure than this. One constraint holds regardless. Any change that makes this project less open breaks a promise already made to the people depending on it, and this project does not do rug pulls. Opening governance further is always available. Closing it back down is not.
