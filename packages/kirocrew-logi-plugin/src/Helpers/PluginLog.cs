namespace Loupedeck.KiroCrewPlugin
{
    using System;

    internal static class PluginLog
    {
        private static PluginLogFile pluginLogFile;

        public static void Init(PluginLogFile logFile)
        {
            logFile.CheckNullArgument(nameof(logFile));
            PluginLog.pluginLogFile = logFile;
        }

        public static void Warning(String text) => PluginLog.pluginLogFile?.Warning(text);

        public static void Error(Exception exception, String text) => PluginLog.pluginLogFile?.Error(exception, text);
    }
}
