namespace Loupedeck.KiroCrewPlugin
{
    using System;
    using System.Collections.Generic;
    using System.Linq;

    internal sealed record AlertEventDefinition(String Name, String DisplayName, String Description);

    // These names are a contract with the Kiro Crew dashboard and with package/events/*.yaml.
    // Options+ stores each user's waveform choice under them, so renaming one resets that choice.
    internal static class AlertEvents
    {
        public const String TurnDone = "turn_done";
        public const String NeedsInput = "needs_input";
        public const String Notification = "notification";

        public static readonly IReadOnlyList<AlertEventDefinition> Definitions = new[]
        {
            new AlertEventDefinition(TurnDone, "Agent finished", "A Kiro Crew conversation finished and is waiting for you."),
            new AlertEventDefinition(NeedsInput, "Agent needs your input", "A Kiro Crew agent asked a question or is waiting for an approval."),
            new AlertEventDefinition(Notification, "New notification", "A new item arrived in the Kiro Crew notification feed."),
        };

        public static readonly IReadOnlyList<String> Names = Definitions.Select(definition => definition.Name).ToArray();
    }
}
