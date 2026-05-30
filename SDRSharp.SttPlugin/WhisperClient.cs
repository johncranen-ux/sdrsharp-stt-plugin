using System;
using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace SDRSharp.SttPlugin
{
    public sealed class WhisperClient : IDisposable
    {
        private string _serverUrl = "http://localhost:9000";
        private string _language  = "en";
        private string _prompt    = "";
        private string _mode      = "maritime";

        public event Action<string>? StatusChanged;

        public string ServerUrl
        {
            get => _serverUrl;
            set => _serverUrl = (value ?? string.Empty).TrimEnd('/');
        }

        public string Language
        {
            get => _language;
            set => _language = value?.Trim() ?? "";
        }

        public string Prompt
        {
            get => _prompt;
            set => _prompt = value?.Trim() ?? "";
        }

        public string Mode
        {
            get => _mode;
            set => _mode = string.IsNullOrWhiteSpace(value) ? "maritime" : value.ToLowerInvariant().Trim();
        }

        public WhisperClient() { }

        public async Task SendAsync(float[] samples, double sampleRate, string? channel = null)
        {
            var serverUrl = _serverUrl;
            var language  = _language;
            var prompt    = _prompt;
            var mode      = _mode;

            if (string.IsNullOrWhiteSpace(serverUrl))
            {
                RaiseStatus("No server URL configured.");
                return;
            }

            RaiseStatus("Sending…");

            try
            {
                var wavBytes = WavBuilder.Build(samples, sampleRate);
                var boundary = "----SttBoundary" + Guid.NewGuid().ToString("N");
                var body     = BuildMultipartBody(boundary, wavBytes, language, prompt);

                if (!TryParseUrl(serverUrl + "/v1/audio/transcriptions",
                                 out var host, out var port, out var path))
                {
                    RaiseStatus("Invalid server URL.");
                    return;
                }

                using var tcp = new TcpClient();
                var connectTask = tcp.ConnectAsync(host, port);
                if (await Task.WhenAny(connectTask, Task.Delay(5_000)).ConfigureAwait(false) != connectTask)
                {
                    RaiseStatus("Connection timed out.");
                    return;
                }
                await connectTask.ConfigureAwait(false);

                tcp.SendTimeout = 10_000;
                var stream = tcp.GetStream();

                var channelHeader = !string.IsNullOrEmpty(channel)
                    ? $"X-Whisper-Channel: {channel}\r\n"
                    : "";

                var requestLine =
                    $"POST {path} HTTP/1.1\r\n" +
                    $"Host: {host}:{port}\r\n" +
                    $"Content-Type: multipart/form-data; boundary={boundary}\r\n" +
                    $"Content-Length: {body.Length}\r\n" +
                    $"X-Whisper-Mode: {mode}\r\n" +
                    channelHeader +
                    "Connection: close\r\n\r\n";

                var headerBytes = Encoding.ASCII.GetBytes(requestLine);
                await stream.WriteAsync(headerBytes, 0, headerBytes.Length).ConfigureAwait(false);
                await stream.WriteAsync(body, 0, body.Length).ConfigureAwait(false);
                await stream.FlushAsync().ConfigureAwait(false);

                using var cts    = new CancellationTokenSource(TimeSpan.FromSeconds(60.0));
                using var reader = new StreamReader(stream, Encoding.UTF8);

                var statusLine = await reader.ReadLineAsync().WaitAsync(cts.Token).ConfigureAwait(false) ?? "";
                var statusCode = statusLine.Length >= 12 ? statusLine.Substring(9, 3) : "???";

                // Skip HTTP headers
                while (true)
                {
                    var line = await reader.ReadLineAsync().WaitAsync(cts.Token).ConfigureAwait(false);
                    if (string.IsNullOrEmpty(line)) break;
                }

                var responseBody = await reader.ReadToEndAsync().WaitAsync(cts.Token).ConfigureAwait(false);

                if (statusCode == "200")
                {
                    var text = ExtractText(responseBody);
                    if (!string.IsNullOrWhiteSpace(text))
                        RaiseStatus(text);
                }
                else
                {
                    var preview = responseBody.Length > 100 ? responseBody[..100] + "…" : responseBody;
                    RaiseStatus($"HTTP {statusCode}: {preview}");
                }
            }
            catch (OperationCanceledException)
            {
                RaiseStatus("Request timed out (60 s).");
            }
            catch (SocketException ex)
            {
                RaiseStatus($"Network error: {ex.Message}");
            }
            catch (IOException ex)
            {
                RaiseStatus($"IO error: {ex.Message}");
            }
            catch (Exception ex)
            {
                RaiseStatus($"Error: {ex.Message}");
            }
        }

        private static bool TryParseUrl(string url, out string host, out int port, out string path)
        {
            host = ""; port = 80; path = "/";
            if (!url.StartsWith("http://", StringComparison.OrdinalIgnoreCase)) return false;

            var rest      = url.Substring(7);
            var slashIdx  = rest.IndexOf('/');
            var authority = slashIdx < 0 ? rest : rest.Substring(0, slashIdx);
            path          = slashIdx < 0 ? "/" : rest.Substring(slashIdx);

            var colonIdx = authority.LastIndexOf(':');
            if (colonIdx < 0)
            {
                host = authority;
                port = 80;
            }
            else
            {
                host = authority.Substring(0, colonIdx);
                if (!int.TryParse(authority.Substring(colonIdx + 1), out port))
                    return false;
            }

            return host.Length > 0;
        }

        private static byte[] BuildMultipartBody(string boundary, byte[] wavBytes,
                                                  string language, string prompt)
        {
            using var ms = new MemoryStream();

            void AddField(string name, string value)
            {
                var part = Encoding.UTF8.GetBytes(
                    $"--{boundary}\r\n" +
                    $"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n" +
                    $"{value}\r\n");
                ms.Write(part, 0, part.Length);
            }

            AddField("temperature", "0");
            if (!string.IsNullOrEmpty(language)) AddField("language", language);
            if (!string.IsNullOrEmpty(prompt))   AddField("initial_prompt", prompt);

            var fileHeader = Encoding.ASCII.GetBytes(
                $"--{boundary}\r\n" +
                "Content-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n" +
                "Content-Type: audio/wav\r\n\r\n");
            ms.Write(fileHeader, 0, fileHeader.Length);
            ms.Write(wavBytes, 0, wavBytes.Length);
            ms.Write(Encoding.ASCII.GetBytes("\r\n"), 0, 2);
            ms.Write(Encoding.ASCII.GetBytes($"--{boundary}--\r\n"), 0,
                     $"--{boundary}--\r\n".Length);

            return ms.ToArray();
        }

        private static string ExtractText(string json)
        {
            try
            {
                int tIdx = json.IndexOf("\"text\"", StringComparison.Ordinal);
                if (tIdx >= 0)
                {
                    int colon = json.IndexOf(':', tIdx);
                    int qo    = json.IndexOf('"', colon + 1);
                    int qc    = json.IndexOf('"', qo + 1);
                    if (qo >= 0 && qc > qo)
                        return json.Substring(qo + 1, qc - qo - 1).Trim();
                }
            }
            catch { }
            return "";
        }

        private void RaiseStatus(string message) => StatusChanged?.Invoke(message);

        public void Dispose() { }
    }
}
