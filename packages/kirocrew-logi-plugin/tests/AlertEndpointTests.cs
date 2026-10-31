namespace Loupedeck.KiroCrewPlugin.Tests
{
    using System;
    using System.Collections.Concurrent;
    using System.Diagnostics;
    using System.IO;
    using System.Net.Sockets;
    using System.Text;
    using System.Threading.Tasks;

    using Xunit;

    public sealed class AlertEndpointTests : IDisposable
    {
        private readonly ConcurrentQueue<String> raisedEvents = new ConcurrentQueue<String>();
        private readonly ConcurrentQueue<String> warnings = new ConcurrentQueue<String>();
        private readonly AlertEndpoint endpoint;

        public AlertEndpointTests()
        {
            this.endpoint = new AlertEndpoint(0, this.raisedEvents.Enqueue, this.warnings.Enqueue, TimeSpan.FromMilliseconds(500));
            this.endpoint.Start();
        }

        public void Dispose() => this.endpoint.Dispose();

        private String Host => $"127.0.0.1:{this.endpoint.Port}";

        [Fact]
        public async Task ADashboardPostRaisesTheEventAndAnswersWithCors()
        {
            String response = await this.SendAsync(
                $"POST /v1/events/turn_done HTTP/1.1\r\nHost: {this.Host}\r\nOrigin: http://localhost:5476\r\nContent-Length: 0\r\n\r\n");

            Assert.StartsWith("HTTP/1.1 202 Accepted\r\n", response);
            Assert.Contains("\r\nAccess-Control-Allow-Origin: http://localhost:5476\r\n", response);
            Assert.Contains("\r\nConnection: close\r\n", response);
            Assert.EndsWith("{\"raised\":\"turn_done\"}", response);
            Assert.Equal(new[] { "turn_done" }, this.raisedEvents.ToArray());
        }

        [Fact]
        public async Task StatusAnswersWithoutRaisingAnything()
        {
            String response = await this.SendAsync($"GET /v1/status HTTP/1.1\r\nHost: {this.Host}\r\n\r\n");

            Assert.StartsWith("HTTP/1.1 200 OK\r\n", response);
            Assert.Contains("\"plugin\":\"KiroCrew\"", response);
            Assert.Empty(this.raisedEvents);
        }

        [Fact]
        public async Task AForeignOriginIsRefusedAndNothingIsRaised()
        {
            String response = await this.SendAsync(
                $"POST /v1/events/turn_done HTTP/1.1\r\nHost: {this.Host}\r\nOrigin: https://evil.example\r\n\r\n");

            Assert.StartsWith("HTTP/1.1 403 Forbidden\r\n", response);
            Assert.DoesNotContain("Access-Control-Allow-Origin", response);
            Assert.Empty(this.raisedEvents);
        }

        [Fact]
        public async Task ARebindingHostHeaderIsRefusedAndNothingIsRaised()
        {
            String response = await this.SendAsync(
                $"POST /v1/events/turn_done HTTP/1.1\r\nHost: evil.example:{this.endpoint.Port}\r\n\r\n");

            Assert.StartsWith("HTTP/1.1 403 Forbidden\r\n", response);
            Assert.Empty(this.raisedEvents);
        }

        [Fact]
        public async Task ARequestCarryingABodyIsStillAnswered()
        {
            String body = new String('x', 32 * 1024);
            String response = await this.SendAsync(
                $"POST /v1/events/notification HTTP/1.1\r\nHost: {this.Host}\r\nContent-Length: {body.Length}\r\n\r\n{body}");

            Assert.StartsWith("HTTP/1.1 202 Accepted\r\n", response);
            Assert.Equal(new[] { "notification" }, this.raisedEvents.ToArray());
        }

        [Fact]
        public async Task AnOversizedHeadIsRefusedWithoutRaising()
        {
            String padding = new String('a', 9000);
            String response = await this.SendAsync(
                $"POST /v1/events/notification HTTP/1.1\r\nHost: {this.Host}\r\nX-Padding: {padding}\r\n\r\n");

            Assert.StartsWith("HTTP/1.1 431 Request Header Fields Too Large\r\n", response);
            Assert.Empty(this.raisedEvents);
        }

        [Fact]
        public async Task ARequestArrivingInPiecesIsReassembled()
        {
            using TcpClient client = new TcpClient();
            await client.ConnectAsync("127.0.0.1", this.endpoint.Port);
            NetworkStream stream = client.GetStream();
            foreach (String piece in new[] { "POST /v1/events/needs", $"_input HTTP/1.1\r\nHost: {this.Host}\r", "\n\r\n" })
            {
                await stream.WriteAsync(Encoding.ASCII.GetBytes(piece));
                await stream.FlushAsync();
                await Task.Delay(50);
            }

            using MemoryStream received = new MemoryStream();
            await stream.CopyToAsync(received).WaitAsync(TimeSpan.FromSeconds(5));

            Assert.StartsWith("HTTP/1.1 202 Accepted\r\n", Encoding.UTF8.GetString(received.ToArray()));
            Assert.Equal(new[] { "needs_input" }, this.raisedEvents.ToArray());
        }

        [Fact]
        public async Task AStalledClientIsCutOffAtTheDeadline()
        {
            using TcpClient client = new TcpClient();
            await client.ConnectAsync("127.0.0.1", this.endpoint.Port);
            NetworkStream stream = client.GetStream();
            await stream.WriteAsync(Encoding.ASCII.GetBytes($"POST /v1/events/turn_done HTTP/1.1\r\nHost: {this.Host}\r\n"));

            Stopwatch elapsed = Stopwatch.StartNew();
            Int32 read = await ReadUntilClosedAsync(stream, TimeSpan.FromSeconds(5));

            Assert.Equal(0, read);
            Assert.InRange(elapsed.Elapsed, TimeSpan.Zero, TimeSpan.FromSeconds(3));
            Assert.Empty(this.raisedEvents);
        }

        [Fact]
        public async Task AFailedRaiseIsReportedAsAServerError()
        {
            using AlertEndpoint failing = new AlertEndpoint(0, _ => throw new InvalidOperationException("Options+ is gone"), this.warnings.Enqueue);
            failing.Start();

            String response = await SendAsync(failing.Port, $"POST /v1/events/turn_done HTTP/1.1\r\nHost: 127.0.0.1:{failing.Port}\r\n\r\n");

            Assert.StartsWith("HTTP/1.1 500 Internal Server Error\r\n", response);
            Assert.Contains(this.warnings, warning => warning.Contains("Options+ is gone", StringComparison.Ordinal));
        }

        [Fact]
        public void ASecondEndpointOnTheSamePortFailsToStart()
        {
            using AlertEndpoint second = new AlertEndpoint(this.endpoint.Port, this.raisedEvents.Enqueue, this.warnings.Enqueue);

            Assert.Throws<SocketException>(() => second.Start());
        }

        [Fact]
        public async Task ADisposedEndpointStopsAcceptingConnections()
        {
            Int32 port = this.endpoint.Port;
            this.endpoint.Dispose();

            using TcpClient client = new TcpClient();
            await Assert.ThrowsAnyAsync<SocketException>(() => client.ConnectAsync("127.0.0.1", port));
        }

        private Task<String> SendAsync(String rawRequest) => SendAsync(this.endpoint.Port, rawRequest);

        private static async Task<String> SendAsync(Int32 port, String rawRequest)
        {
            using TcpClient client = new TcpClient();
            await client.ConnectAsync("127.0.0.1", port);
            NetworkStream stream = client.GetStream();
            await stream.WriteAsync(Encoding.ASCII.GetBytes(rawRequest));
            using MemoryStream received = new MemoryStream();
            await stream.CopyToAsync(received).WaitAsync(TimeSpan.FromSeconds(5));
            return Encoding.UTF8.GetString(received.ToArray());
        }

        private static async Task<Int32> ReadUntilClosedAsync(NetworkStream stream, TimeSpan timeout)
        {
            Byte[] buffer = new Byte[1024];
            Int32 total = 0;
            while (true)
            {
                Int32 read = await stream.ReadAsync(buffer).AsTask().WaitAsync(timeout);
                if (read == 0)
                {
                    return total;
                }

                total += read;
            }
        }
    }
}
