namespace Loupedeck.KiroCrewPlugin.Tests
{
    using System;
    using System.Collections.Generic;
    using System.IO;
    using System.Linq;
    using System.Text.RegularExpressions;

    using Xunit;

    public class PackageDefinitionTests
    {
        // The waveform names Logitech documents for haptic event mappings.
        private static readonly HashSet<String> DocumentedWaveforms = new HashSet<String>
        {
            "sharp_state_change", "damp_state_change", "sharp_collision", "damp_collision", "subtle_collision",
            "happy_alert", "angry_alert", "completed", "square", "wave", "firework", "mad", "knock", "jingle", "ringing",
        };

        private static String ReadPackageFile(String relativePath) =>
            File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "package", "events", relativePath));

        [Fact]
        public void TheEventSourceListsExactlyTheEventsTheCodeRegisters()
        {
            String yaml = ReadPackageFile("DefaultEventSource.yaml");
            MatchCollection entries = Regex.Matches(yaml, @"- name: (\S+)\s+displayName: (.+)\s+description: (.+)");

            (String, String, String)[] fromYaml = entries
                .Select(entry => (entry.Groups[1].Value, entry.Groups[2].Value.Trim(), entry.Groups[3].Value.Trim()))
                .ToArray();
            (String, String, String)[] fromCode = AlertEvents.Definitions
                .Select(definition => (definition.Name, definition.DisplayName, definition.Description))
                .ToArray();
            Assert.Equal(fromCode, fromYaml);
        }

        [Fact]
        public void EveryEventMapsToADocumentedWaveform()
        {
            String yaml = ReadPackageFile(Path.Combine("extra", "eventMapping.yaml"));
            Dictionary<String, String> defaults = Regex.Matches(yaml, @"^  (\w+):\s+DEFAULT: (\w+)", RegexOptions.Multiline)
                .ToDictionary(entry => entry.Groups[1].Value, entry => entry.Groups[2].Value);

            Assert.Equal(AlertEvents.Names.OrderBy(name => name), defaults.Keys.OrderBy(name => name));
            Assert.All(defaults.Values, waveform => Assert.Contains(waveform, DocumentedWaveforms));
        }
    }
}
