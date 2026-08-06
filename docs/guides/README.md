# Guides

Task-oriented documentation: how to install, run, and operate Kiro Crew. For how the
system is built, see [../architecture/](../architecture/README.md).

| Guide | Covers |
|---|---|
| [install.md](install.md) | Installing and building Kiro Crew: source, wheel, and first run. |
| [windows-install.md](windows-install.md) | Native Windows setup, and the per-feature status on Windows. |
| [docker.md](docker.md) | Running Kiro Crew as a container. |
| [remote-and-mobile.md](remote-and-mobile.md) | Running 24/7 on a remote host, keeping it alive as a service, and reaching it from a phone over a tunnel. |
| [cloud-instance-ssm-vs-ssh.md](cloud-instance-ssm-vs-ssh.md) | How a cloud-launched instance is reached through the Instances hub: the native AWS SSM transport vs the legacy SSH-over-`ProxyCommand` path. |
| [remote-crew-on-ec2.md](remote-crew-on-ec2.md) | Reaching a Remote Crew gateway on EC2 over SSH or AWS SSM, plus the common EC2 setup gotchas (sandbox backend, linger, port/tunnel matching). |
| [slack-setup.md](slack-setup.md) | Creating and configuring the Slack app. |

`assets/` holds the copy-pasteable service unit, launchd plist, and setup script
that [remote-and-mobile.md](remote-and-mobile.md) refers to, plus an example
`security_policy.json` that
[governance.md](../system-specs/modules/governance.md) refers to.

End-user feature documentation is not here: it ships in the package under
[`../../src/kiro_crew/docs/`](../../src/kiro_crew/docs/README.md).
