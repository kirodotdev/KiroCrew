namespace Loupedeck.KiroCrewPlugin.Tests
{
    using System;

    using Xunit;

    public class HttpRequestHeadTests
    {
        [Fact]
        public void ParsesTheFieldsTheRouterNeeds()
        {
            HttpRequestHead head = HttpRequestHead.Parse(String.Join(
                "\r\n",
                "POST /v1/events/turn_done?from=dashboard HTTP/1.1",
                "host: 127.0.0.1:41870",
                "ORIGIN: http://localhost:5476",
                "Access-Control-Request-Private-Network: true",
                "Content-Length: 0"));

            Assert.Null(head.Problem);
            AlertRequest request = head.ToAlertRequest();
            Assert.Equal("POST", request.Method);
            Assert.Equal("/v1/events/turn_done", request.Path);
            Assert.Equal("127.0.0.1:41870", request.Host);
            Assert.Equal("http://localhost:5476", request.Origin);
            Assert.True(request.AsksForPrivateNetworkAccess);
        }

        [Fact]
        public void AMissingHostIsLeftForTheRouterToRefuse()
        {
            HttpRequestHead head = HttpRequestHead.Parse("POST /v1/events/turn_done HTTP/1.1");

            Assert.Null(head.Problem);
            Assert.Null(head.Host);
        }

        [Theory]
        [InlineData("")]
        [InlineData("POST /v1/events/turn_done")]
        [InlineData("POST /v1/events/turn_done HTTP/2")]
        [InlineData("POST  /v1/events/turn_done HTTP/1.1")]
        public void AMalformedRequestLineIsRefused(String requestLine)
        {
            AssertRefused(requestLine + "\r\nHost: 127.0.0.1:41870", 400);
        }

        [Fact]
        public void AnAbsoluteFormTargetIsRefused()
        {
            AssertRefused("POST http://127.0.0.1:41870/v1/events/turn_done HTTP/1.1\r\nHost: 127.0.0.1:41870", 400);
        }

        [Fact]
        public void DuplicateHostHeadersAreRefused()
        {
            AssertRefused("POST /v1/events/turn_done HTTP/1.1\r\nHost: 127.0.0.1:41870\r\nHost: evil.example", 400);
        }

        [Fact]
        public void AHeaderWithoutANameIsRefused()
        {
            AssertRefused("POST /v1/events/turn_done HTTP/1.1\r\nHost: 127.0.0.1:41870\r\n: value", 400);
        }

        private static void AssertRefused(String headText, Int32 expectedStatusCode)
        {
            HttpRequestHead head = HttpRequestHead.Parse(headText);

            Assert.NotNull(head.Problem);
            Assert.Equal(expectedStatusCode, head.Problem.StatusCode);
            Assert.Null(head.Problem.EventToRaise);
        }
    }
}
