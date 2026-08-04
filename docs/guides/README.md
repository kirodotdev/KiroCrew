# Guides

Task-oriented documentation: how to install, run, and operate Kiro Crew. For how the
system is built, see [../architecture/](../architecture/README.md).

| Guide | Covers |
|---|---|
| [install.md](install.md) | Installing and building Kiro Crew: source, wheel, and first run. |
| [windows-install.md](windows-install.md) | Native Windows setup, and the per-feature status on Windows. |
| [docker.md](docker.md) | Running Kiro Crew as a container. |
| [remote-and-mobile.md](remote-and-mobile.md) | Running 24/7 on a remote host, keeping it alive as a service, and reaching it from a phone over a tunnel. |
| [slack-setup.md](slack-setup.md) | Creating and configuring the Slack app. |

`assets/` holds the copy-pasteable service unit, launchd plist, and setup script
that [remote-and-mobile.md](remote-and-mobile.md) refers to.

End-user feature documentation is not here: it ships in the package under
[`../../src/kiro_crew/docs/`](../../src/kiro_crew/docs/README.md).
