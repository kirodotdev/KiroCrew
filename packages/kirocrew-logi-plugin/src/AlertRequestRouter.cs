namespace Loupedeck.KiroCrewPlugin
{
    using System;
    using System.Collections.Generic;
    using System.Linq;
    using System.Text.Json;

    internal sealed record AlertRequest(String Method, String Path, String Host, String Origin, Boolean AsksForPrivateNetworkAccess);

    internal sealed record AlertResponse(
        Int32 StatusCode,
        String JsonBody,
        String AllowedOrigin,
        String EventToRaise = null,
        Boolean IsPreflight = false,
        Boolean AllowsPrivateNetworkAccess = false);

    internal static class AlertRequestRouter
    {
        public const Int32 ApiVersion = 1;
        public const String PluginName = "KiroCrew"; // brand-ok: wire identifier the dashboard matches
        public const String StatusPath = "/v1/status";
        public const String EventsPathPrefix = "/v1/events/";

        // The hosts a dashboard page on this computer can have. Uri.Host keeps the brackets of an IPv6 literal.
        private static readonly HashSet<String> LoopbackPageHosts = new HashSet<String>(StringComparer.Ordinal) { "localhost", "127.0.0.1", "[::1]" };

        public static AlertResponse Route(AlertRequest request, IReadOnlySet<String> acceptedHosts)
        {
            // A web page on another host name that resolves to 127.0.0.1 (DNS rebinding) still sends its own Host.
            if (request.Host is null || !acceptedHosts.Contains(request.Host))
            {
                return Refusal(403, "host not allowed");
            }

            Boolean hasOrigin = !String.IsNullOrEmpty(request.Origin);
            String allowedOrigin = null;
            if (hasOrigin && !TryNormalizeLoopbackWebOrigin(request.Origin, out allowedOrigin))
            {
                return Refusal(403, "origin not allowed");
            }

            Boolean isStatusPath = request.Path == StatusPath;
            Boolean isEventPath = request.Path.StartsWith(EventsPathPrefix, StringComparison.Ordinal);
            if (!isStatusPath && !isEventPath)
            {
                return Json(404, new { error = "not found" }, allowedOrigin);
            }

            if (request.Method == "OPTIONS")
            {
                return new AlertResponse(
                    204,
                    JsonBody: null,
                    allowedOrigin,
                    IsPreflight: true,
                    AllowsPrivateNetworkAccess: hasOrigin && request.AsksForPrivateNetworkAccess);
            }

            if (isStatusPath)
            {
                return request.Method == "GET"
                    ? Json(200, new { plugin = PluginName, api = ApiVersion, events = AlertEvents.Names }, allowedOrigin)
                    : Json(405, new { error = "method not allowed" }, allowedOrigin);
            }

            if (request.Method != "POST")
            {
                return Json(405, new { error = "method not allowed" }, allowedOrigin);
            }

            String eventName = request.Path.Substring(EventsPathPrefix.Length);
            if (!AlertEvents.Names.Contains(eventName))
            {
                return Json(404, new { error = "unknown event", events = AlertEvents.Names }, allowedOrigin);
            }

            return Json(202, new { raised = eventName }, allowedOrigin) with { EventToRaise = eventName };
        }

        public static AlertResponse Refusal(Int32 statusCode, String reason) => Json(statusCode, new { error = reason }, allowedOrigin: null);

        private static AlertResponse Json(Int32 statusCode, Object body, String allowedOrigin) =>
            new AlertResponse(statusCode, JsonSerializer.Serialize(body), allowedOrigin);

        // Echoing the normalized form, never the raw header, keeps request bytes out of the response headers.
        private static Boolean TryNormalizeLoopbackWebOrigin(String origin, out String normalizedOrigin)
        {
            normalizedOrigin = null;
            if (!Uri.TryCreate(origin, UriKind.Absolute, out Uri originUri))
            {
                return false;
            }

            Boolean isWebScheme = (originUri.Scheme == Uri.UriSchemeHttp) || (originUri.Scheme == Uri.UriSchemeHttps);
            if (!isWebScheme || !LoopbackPageHosts.Contains(originUri.Host))
            {
                return false;
            }

            normalizedOrigin = originUri.GetLeftPart(UriPartial.Authority);
            return true;
        }
    }
}
