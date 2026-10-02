namespace Loupedeck.KiroCrewPlugin
{
    using System;
    using System.Net.Sockets;
    using System.Threading.Tasks;

    // Options+ owns the mouse and each user's waveform choice per event, so Kiro Crew can make the
    // mouse buzz without ever holding the macOS Input Monitoring permission itself.
    public class KiroCrewPlugin : Plugin
    {
        private AlertEndpoint alertEndpoint;

        public override Boolean UsesApplicationApiOnly => true;

        public override Boolean HasNoApplication => true;

        public KiroCrewPlugin() => PluginLog.Init(this.Log);

        public override void Load()
        {
            foreach (AlertEventDefinition definition in AlertEvents.Definitions)
            {
                this.PluginEvents.AddEvent(definition.Name, definition.DisplayName, definition.Description);
            }

            this.alertEndpoint = new AlertEndpoint(AlertEndpoint.DefaultPort, this.RaiseAlertEvent, PluginLog.Warning);
            try
            {
                this.alertEndpoint.Start();
                this.OnPluginStatusChanged(Loupedeck.PluginStatus.Normal, null);
            }
            catch (SocketException exception)
            {
                PluginLog.Error(exception, $"Could not listen on 127.0.0.1:{AlertEndpoint.DefaultPort}");
                this.OnPluginStatusChanged(
                    Loupedeck.PluginStatus.Error,
                    $"Port {AlertEndpoint.DefaultPort} is already in use, so Kiro Crew alerts cannot reach this plugin.");
            }
        }

        public override void Unload() => this.alertEndpoint?.Dispose();

        // Raised off the connection's thread so a slow hand-off to Options+ never delays the HTTP answer.
        private void RaiseAlertEvent(String eventName) =>
            Task.Run(() =>
            {
                try
                {
                    this.PluginEvents.RaiseEvent(eventName);
                }
                catch (Exception exception)
                {
                    PluginLog.Error(exception, $"Raising {eventName} failed");
                }
            });
    }
}
