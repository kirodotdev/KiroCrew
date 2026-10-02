namespace Loupedeck.KiroCrewPlugin
{
    using System;

    internal sealed class HttpRequestHead
    {
        public String Method { get; private init; }

        public String Path { get; private init; }

        public String Host { get; private init; }

        public String Origin { get; private init; }

        public Boolean AsksForPrivateNetworkAccess { get; private init; }

        public AlertResponse Problem { get; private init; }

        public AlertRequest ToAlertRequest() =>
            new AlertRequest(this.Method, this.Path, this.Host, this.Origin, this.AsksForPrivateNetworkAccess);

        public static HttpRequestHead Failed(AlertResponse problem) => new HttpRequestHead { Problem = problem };

        // Parses the request line and headers, without the terminating blank line. Request bodies are
        // never read, because the API takes none. Anything ambiguous is refused rather than guessed.
        public static HttpRequestHead Parse(String headText)
        {
            String[] lines = headText.Split("\r\n");
            String[] requestLine = lines[0].Split(' ');
            if ((requestLine.Length != 3) || !requestLine[2].StartsWith("HTTP/1.", StringComparison.Ordinal))
            {
                return Failed(AlertRequestRouter.Refusal(400, "malformed request line"));
            }

            String target = requestLine[1];
            if (!target.StartsWith('/'))
            {
                return Failed(AlertRequestRouter.Refusal(400, "request target must be a path"));
            }

            String host = null;
            String origin = null;
            Boolean asksForPrivateNetworkAccess = false;
            for (Int32 index = 1; index < lines.Length; index++)
            {
                String line = lines[index];
                Int32 colon = line.IndexOf(':');
                if (colon <= 0)
                {
                    return Failed(AlertRequestRouter.Refusal(400, "malformed header"));
                }

                String name = line.Substring(0, colon).Trim();
                String value = line.Substring(colon + 1).Trim();
                if (name.Equals("Host", StringComparison.OrdinalIgnoreCase))
                {
                    if (host != null)
                    {
                        return Failed(AlertRequestRouter.Refusal(400, "duplicate Host header"));
                    }

                    host = value;
                }
                else if (name.Equals("Origin", StringComparison.OrdinalIgnoreCase))
                {
                    origin = value;
                }
                else if (name.Equals("Access-Control-Request-Private-Network", StringComparison.OrdinalIgnoreCase))
                {
                    asksForPrivateNetworkAccess = value.Equals("true", StringComparison.OrdinalIgnoreCase);
                }
            }

            Int32 queryStart = target.IndexOf('?');
            return new HttpRequestHead
            {
                Method = requestLine[0],
                Path = (queryStart >= 0) ? target.Substring(0, queryStart) : target,
                Host = host,
                Origin = origin,
                AsksForPrivateNetworkAccess = asksForPrivateNetworkAccess,
            };
        }
    }
}
