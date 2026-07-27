using System;
using System.Globalization;
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

        // Returns the transcribed text on success, or "" on any failure/timeout/empty result.
        public async Task<string> SendAsync(float[] samples, double sampleRate, string? channel = null)
        {
            var serverUrl = _serverUrl;
            var language  = _language;
            var prompt    = _prompt;
            var mode      = _mode;

            if (string.IsNullOrWhiteSpace(serverUrl))
            {
                RaiseStatus("No server URL configured.");
                return "";
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
                    return "";
                }

                using var tcp = new TcpClient();
                var connectTask = tcp.ConnectAsync(host, port);
                if (await Task.WhenAny(connectTask, Task.Delay(TimeSpan.FromSeconds(5.0))).ConfigureAwait(false) != connectTask)
                {
                    RaiseStatus("Connection timed out.");
                    return "";
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

                // Covers the whole request/response exchange, not just the read: NetworkStream
                // .WriteAsync does not honor TcpClient.SendTimeout (that only applies to the
                // synchronous Write path), so an unbounded write here could hang indefinitely
                // with no timeout at all if the connection stalls.
                using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(60.0));

                var headerBytes = Encoding.ASCII.GetBytes(requestLine);
                await stream.WriteAsync(headerBytes, 0, headerBytes.Length, cts.Token).ConfigureAwait(false);
                await stream.WriteAsync(body, 0, body.Length, cts.Token).ConfigureAwait(false);
                await stream.FlushAsync(cts.Token).ConfigureAwait(false);

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
                    return text;
                }
                else
                {
                    var preview = responseBody.Length > 100 ? responseBody[..100] + "…" : responseBody;
                    RaiseStatus($"HTTP {statusCode}: {preview}");
                    return "";
                }
            }
            catch (OperationCanceledException)
            {
                RaiseStatus("Request timed out (60 s).");
                return "";
            }
            catch (SocketException ex)
            {
                RaiseStatus($"Network error: {ex.Message}");
                return "";
            }
            catch (IOException ex)
            {
                RaiseStatus($"IO error: {ex.Message}");
                return "";
            }
            catch (Exception ex)
            {
                RaiseStatus($"Error: {ex.Message}");
                return "";
            }
        }

        internal static bool TryParseUrl(string url, out string host, out int port, out string path)
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
            AddField("response_format", "json");
            if (!string.IsNullOrEmpty(language)) AddField("language", language);
            if (!string.IsNullOrEmpty(prompt))
            {
                // whisper.cpp's /inference reads "prompt", not "initial_prompt" (which is
                // a whisper.cpp CLI-only flag name and silently ignored by the server).
                AddField("prompt", prompt);
                AddField("carry_initial_prompt", "true");
            }

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

        // Hand-rolled rather than System.Text.Json: that assembly ships with a version tied
        // to the target framework, and SDR# hosts plugins on .NET 8 regardless of what this
        // project targets — a plugin compiled against System.Text.Json 9.x fails to load it
        // at runtime ("Could not load file or assembly 'System.Text.Json, Version=9.0.0.0'").
        // Correctly handles the JSON string-escape sequences the old substring version broke on.
        internal static string ExtractText(string json)
        {
            try
            {
                int tIdx = json.IndexOf("\"text\"", StringComparison.Ordinal);
                if (tIdx < 0) return "";

                int colon = json.IndexOf(':', tIdx + 6);
                if (colon < 0) return "";

                int i = colon + 1;
                while (i < json.Length && char.IsWhiteSpace(json[i])) i++;
                if (i >= json.Length || json[i] != '"') return "";
                i++; // skip opening quote

                var sb = new StringBuilder();
                while (i < json.Length && json[i] != '"')
                {
                    char c = json[i];
                    if (c == '\\' && i + 1 < json.Length)
                    {
                        char next = json[i + 1];
                        switch (next)
                        {
                            case '"':  sb.Append('"');  i += 2; break;
                            case '\\': sb.Append('\\'); i += 2; break;
                            case '/':  sb.Append('/');  i += 2; break;
                            case 'b':  sb.Append('\b'); i += 2; break;
                            case 'f':  sb.Append('\f'); i += 2; break;
                            case 'n':  sb.Append('\n'); i += 2; break;
                            case 'r':  sb.Append('\r'); i += 2; break;
                            case 't':  sb.Append('\t'); i += 2; break;
                            case 'u' when i + 5 < json.Length &&
                                          ushort.TryParse(json.AsSpan(i + 2, 4), NumberStyles.HexNumber,
                                                           CultureInfo.InvariantCulture, out var code):
                                sb.Append((char)code);
                                i += 6;
                                break;
                            default:
                                sb.Append(next);
                                i += 2;
                                break;
                        }
                    }
                    else
                    {
                        sb.Append(c);
                        i++;
                    }
                }
                return sb.ToString().Trim();
            }
            catch { }
            return "";
        }

        private void RaiseStatus(string message) => StatusChanged?.Invoke(message);

        public void Dispose() { }
    }
}
