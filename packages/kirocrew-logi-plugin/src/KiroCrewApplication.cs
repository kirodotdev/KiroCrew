namespace Loupedeck.KiroCrewPlugin
{
    using System;

    // The Logi plugin loader requires one ClientApplication per plugin. Kiro Crew talks to the plugin
    // over loopback HTTP instead, so it links to no process or bundle.
    public class KiroCrewApplication : ClientApplication
    {
        protected override String GetProcessName() => "";

        protected override String GetBundleName() => "";

        public override ClientApplicationStatus GetApplicationStatus() => ClientApplicationStatus.Unknown;
    }
}
