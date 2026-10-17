namespace Loupedeck.KiroCrewPlugin
{
    using System;
    using System.Collections.Generic;
    using System.IO;
    using System.Net;
    using System.Net.Sockets;
    using System.Text;
    using System.Threading;
    using System.Threading.Tasks;

    // A deliberately small HTTP/1.1 endpoint bound to 127.0.0.1. HttpListener is avoided because on
    // Windows it needs a URL reservation that the non-admin Logi Plugin Service process cannot make.
    internal sealed class AlertEndpoint : IDisposable
    {
        public const Int32 DefaultPort = 41870;

        private const Int32 MaxRequestHeadBytes = 8 * 1024;
        private static readonly Byte[] HeadTerminator = Encoding.ASCII.GetBytes("\r\n\r\n");

        private readonly TcpListener listener;
        private readonly Action<String> raiseEvent;
        private readonly Action<String> logWarning;
        private readonly TimeSpan connectionDeadline;
        private readonly CancellationTokenSource stopping = new CancellationTokenSource();
        private IReadOnlySet<String> acceptedHosts = new HashSet<String>();

        public AlertEndpoint(Int32 port, Action<String> raiseEvent, Action<String> logWarning, TimeSpan? connectionDeadline = null)
        {
            this.listener = new TcpListener(IPAddress.Loopback, port);
            this.raiseEvent = raiseEvent;
            this.logWarning = logWarning;
            this.connectionDeadline = connectionDeadline ?? TimeSpan.FromSeconds(5);
        }

        public Int32 Port => ((IPEndPoint)this.listener.LocalEndpoint).Port;

        // Throws SocketException when another process already holds the port.
        public void Start()
        {
            this.listener.Start();
            Int32 boundPort = this.Port;
            this.acceptedHosts = new HashSet<String>(StringComparer.OrdinalIgnoreCase) { $"127.0.0.1:{boundPort}", $"localhost:{boundPort}" };
            _ = Task.Run(this.AcceptConnectionsAsync);
        }

        public void Dispose()
        {
            this.stopping.Cancel();
            this.listener.Stop();
        }

        private async Task AcceptConnectionsAsync()
        {
            while (!this.stopping.IsCancellationRequested)
            {
                TcpClient client;
                try
                {
                    client = await this.listener.AcceptTcpClientAsync(this.stopping.Token).ConfigureAwait(false);
                }
                catch (Exception) when (this.stopping.IsCancellationRequested)
                {
                    return;
                }
                catch (SocketException exception)
                {
                    this.logWarning($"Accepting an alert connection failed: {exception.SocketErrorCode}");
                    continue;
                }
                catch (Exception exception)
                {
                    this.logWarning($"The alert endpoint stopped accepting connections: {exception.Message}");
                    return;
                }

                _ = this.ServeConnectionAsync(client);
            }
        }

        private async Task ServeConnectionAsync(TcpClient client)
        {
            using (client)
            using (CancellationTokenSource deadline = CancellationTokenSource.CreateLinkedTokenSource(this.stopping.Token))
            {
                deadline.CancelAfter(this.connectionDeadline);
                try
                {
                    NetworkStream stream = client.GetStream();
                    AlertResponse response = await this.AnswerAsync(stream, deadline.Token).ConfigureAwait(false);
                    await WriteResponseAsync(stream, response, deadline.Token).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    // The client stalled past the deadline, or the plugin is unloading.
                }
                catch (IOException)
                {
                    // The client hung up mid-request.
                }
                catch (Exception exception)
                {
                    this.logWarning($"Serving an alert request failed: {exception.Message}");
                }
            }
        }

        private async Task<AlertResponse> AnswerAsync(NetworkStream stream, CancellationToken cancellation)
        {
            HttpRequestHead head = await ReadRequestHeadAsync(stream, cancellation).ConfigureAwait(false);
            if (head.Problem != null)
            {
                return head.Problem;
            }

            AlertResponse response = AlertRequestRouter.Route(head.ToAlertRequest(), this.acceptedHosts);
            if (response.EventToRaise is null)
            {
                return response;
            }

            try
            {
                this.raiseEvent(response.EventToRaise);
                return response;
            }
            catch (Exception exception)
            {
                this.logWarning($"Raising {response.EventToRaise} failed: {exception.Message}");
                return AlertRequestRouter.Refusal(500, "raising the event failed");
            }
        }

        private static async Task<HttpRequestHead> ReadRequestHeadAsync(NetworkStream stream, CancellationToken cancellation)
        {
            Byte[] buffer = new Byte[MaxRequestHeadBytes];
            Int32 filled = 0;
            while (true)
            {
                Int32 headEnd = buffer.AsSpan(0, filled).IndexOf(HeadTerminator);
                if (headEnd >= 0)
                {
                    return HttpRequestHead.Parse(Encoding.ASCII.GetString(buffer, 0, headEnd));
                }

                if (filled == buffer.Length)
                {
                    return HttpRequestHead.Failed(AlertRequestRouter.Refusal(431, "request head too large"));
                }

                Int32 read = await stream.ReadAsync(buffer.AsMemory(filled), cancellation).ConfigureAwait(false);
                if (read == 0)
                {
                    return HttpRequestHead.Failed(AlertRequestRouter.Refusal(400, "incomplete request"));
                }

                filled += read;
            }
        }

        private static async Task WriteResponseAsync(NetworkStream stream, AlertResponse response, CancellationToken cancellation)
        {
            Byte[] body = (response.JsonBody is null) ? Array.Empty<Byte>() : Encoding.UTF8.GetBytes(response.JsonBody);
            StringBuilder head = new StringBuilder();
            head.Append("HTTP/1.1 ").Append(response.StatusCode).Append(' ').Append(ReasonPhrase(response.StatusCode)).Append("\r\n");
            if (body.Length > 0)
            {
                head.Append("Content-Type: application/json; charset=utf-8\r\n");
            }

            head.Append("Content-Length: ").Append(body.Length).Append("\r\n");
            head.Append("Cache-Control: no-store\r\n");
            head.Append("Connection: close\r\n");
            if (response.AllowedOrigin != null)
            {
                head.Append("Access-Control-Allow-Origin: ").Append(response.AllowedOrigin).Append("\r\n");
                head.Append("Vary: Origin\r\n");
            }

            if (response.IsPreflight)
            {
                head.Append("Access-Control-Allow-Methods: GET, POST\r\n");
                head.Append("Access-Control-Allow-Headers: Content-Type\r\n");
                head.Append("Access-Control-Max-Age: 600\r\n");
            }

            if (response.AllowsPrivateNetworkAccess)
            {
                head.Append("Access-Control-Allow-Private-Network: true\r\n");
            }

            head.Append("\r\n");
            await stream.WriteAsync(Encoding.ASCII.GetBytes(head.ToString()), cancellation).ConfigureAwait(false);
            if (body.Length > 0)
            {
                await stream.WriteAsync(body, cancellation).ConfigureAwait(false);
            }
        }

        private static String ReasonPhrase(Int32 statusCode) => statusCode switch
        {
            200 => "OK",
            202 => "Accepted",
            204 => "No Content",
            400 => "Bad Request",
            403 => "Forbidden",
            404 => "Not Found",
            405 => "Method Not Allowed",
            431 => "Request Header Fields Too Large",
            _ => "Internal Server Error",
        };
    }
}
