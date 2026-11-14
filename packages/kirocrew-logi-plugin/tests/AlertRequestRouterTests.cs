namespace Loupedeck.KiroCrewPlugin.Tests
{
    using System;
    using System.Collections.Generic;
    using System.Linq;
    using System.Text.Json;

    using Xunit;

    public class AlertRequestRouterTests
    {
        private const String EndpointHost = "127.0.0.1:41870";
        private const String DashboardOrigin = "http://localhost:5476";

        private static readonly IReadOnlySet<String> AcceptedHosts =
            new HashSet<String>(StringComparer.OrdinalIgnoreCase) { EndpointHost, "localhost:41870" };

        private static AlertResponse Route(
            String method,
            String path,
            String origin = null,
            String host = EndpointHost,
            Boolean asksForPrivateNetworkAccess = false) =>
            AlertRequestRouter.Route(new AlertRequest(method, path, host, origin, asksForPrivateNetworkAccess), AcceptedHosts);

        [Fact]
        public void StatusNamesThePluginTheApiVersionAndEveryEvent()
        {
            AlertResponse response = Route("GET", "/v1/status");

            Assert.Equal(200, response.StatusCode);
            Assert.Null(response.EventToRaise);
            using JsonDocument body = JsonDocument.Parse(response.JsonBody);
            Assert.Equal("KiroCrew", body.RootElement.GetProperty("plugin").GetString()); // brand-ok: the wire value the dashboard matches
            Assert.Equal(1, body.RootElement.GetProperty("api").GetInt32());
            String[] events = body.RootElement.GetProperty("events").EnumerateArray().Select(element => element.GetString()).ToArray();
            Assert.Equal(new[] { "turn_done", "needs_input", "notification" }, events);
        }

        [Theory]
        [InlineData("turn_done")]
        [InlineData("needs_input")]
        [InlineData("notification")]
        public void PostingAKnownEventRaisesIt(String eventName)
        {
            AlertResponse response = Route("POST", "/v1/events/" + eventName, DashboardOrigin);

            Assert.Equal(202, response.StatusCode);
            Assert.Equal(eventName, response.EventToRaise);
        }

        [Theory]
        [InlineData("/v1/events/")]
        [InlineData("/v1/events/bogus")]
        [InlineData("/v1/events/turn_done/extra")]
        [InlineData("/v1/events/TURN_DONE")]
        public void UnknownEventsAreNotRaised(String path)
        {
            AlertResponse response = Route("POST", path, DashboardOrigin);

            Assert.Equal(404, response.StatusCode);
            Assert.Null(response.EventToRaise);
        }

        [Theory]
        [InlineData("http://localhost:5476")]
        [InlineData("http://127.0.0.1:5476")]
        [InlineData("http://[::1]:5476")]
        [InlineData("https://localhost")]
        public void EveryLoopbackDashboardOriginIsAllowedAndEchoed(String origin)
        {
            AlertResponse response = Route("POST", "/v1/events/turn_done", origin);

            Assert.Equal(202, response.StatusCode);
            Assert.Equal(origin, response.AllowedOrigin);
        }

        [Fact]
        public void TheOriginIsEchoedInNormalizedForm()
        {
            AlertResponse response = Route("POST", "/v1/events/turn_done", "HTTP://LOCALHOST:5476");

            Assert.Equal(202, response.StatusCode);
            Assert.Equal("http://localhost:5476", response.AllowedOrigin);
        }

        [Theory]
        [InlineData("https://evil.example")]
        [InlineData("http://localhost.evil.example:5476")]
        [InlineData("http://127.0.0.1.evil.example")]
        [InlineData("null")]
        [InlineData("file://")]
        [InlineData("chrome-extension://abcdefghijklmnop")]
        [InlineData("ws://localhost:5476")]
        [InlineData("http://[::2]:5476")]
        public void ForeignOriginsAreRefusedWithoutRaisingOrCors(String origin)
        {
            AlertResponse response = Route("POST", "/v1/events/turn_done", origin);

            Assert.Equal(403, response.StatusCode);
            Assert.Null(response.EventToRaise);
            Assert.Null(response.AllowedOrigin);
        }

        [Theory]
        [InlineData("evil.example:41870")]
        [InlineData("127.0.0.1:9999")]
        [InlineData("127.0.0.1")]
        [InlineData(null)]
        public void RequestsAddressedToAnotherHostAreRefused(String host)
        {
            AlertResponse response = Route("POST", "/v1/events/turn_done", DashboardOrigin, host);

            Assert.Equal(403, response.StatusCode);
            Assert.Null(response.EventToRaise);
            Assert.Null(response.AllowedOrigin);
        }

        [Fact]
        public void TheLocalhostSpellingOfTheEndpointIsAccepted()
        {
            AlertResponse response = Route("POST", "/v1/events/turn_done", DashboardOrigin, "LOCALHOST:41870");

            Assert.Equal(202, response.StatusCode);
        }

        [Fact]
        public void ARequestWithoutAnOriginIsServedWithoutCorsHeaders()
        {
            AlertResponse response = Route("POST", "/v1/events/notification");

            Assert.Equal(202, response.StatusCode);
            Assert.Equal("notification", response.EventToRaise);
            Assert.Null(response.AllowedOrigin);
        }

        [Fact]
        public void ADashboardPreflightIsAnsweredWithoutRaising()
        {
            AlertResponse response = Route("OPTIONS", "/v1/events/turn_done", DashboardOrigin);

            Assert.Equal(204, response.StatusCode);
            Assert.True(response.IsPreflight);
            Assert.Equal(DashboardOrigin, response.AllowedOrigin);
            Assert.Null(response.EventToRaise);
            Assert.False(response.AllowsPrivateNetworkAccess);
        }

        [Fact]
        public void PrivateNetworkAccessIsGrantedOnlyWhenAskedForByAnAllowedOrigin()
        {
            Assert.True(Route("OPTIONS", "/v1/events/turn_done", DashboardOrigin, asksForPrivateNetworkAccess: true).AllowsPrivateNetworkAccess);
            Assert.False(Route("OPTIONS", "/v1/events/turn_done", origin: null, asksForPrivateNetworkAccess: true).AllowsPrivateNetworkAccess);
            Assert.Equal(403, Route("OPTIONS", "/v1/events/turn_done", "https://evil.example", asksForPrivateNetworkAccess: true).StatusCode);
        }

        [Theory]
        [InlineData("GET", "/v1/events/turn_done")]
        [InlineData("PUT", "/v1/events/turn_done")]
        [InlineData("POST", "/v1/status")]
        [InlineData("post", "/v1/events/turn_done")]
        public void WrongMethodsAreRejectedWithoutRaising(String method, String path)
        {
            AlertResponse response = Route(method, path, DashboardOrigin);

            Assert.Equal(405, response.StatusCode);
            Assert.Null(response.EventToRaise);
        }

        [Theory]
        [InlineData("/")]
        [InlineData("/v1/status/")]
        [InlineData("/haptic/completed")]
        public void UnknownPathsAreNotFound(String path)
        {
            AlertResponse response = Route("GET", path, DashboardOrigin);

            Assert.Equal(404, response.StatusCode);
            Assert.Null(response.EventToRaise);
        }
    }
}
