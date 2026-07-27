using SDRSharp.SttPlugin;

namespace SDRSharp.SttPlugin.Tests;

public class WhisperClientTests
{
    [Theory]
    [InlineData("http://localhost:9000/v1/audio/transcriptions", "localhost", 9000, "/v1/audio/transcriptions")]
    [InlineData("http://localhost/path", "localhost", 80, "/path")]
    [InlineData("http://localhost:9000", "localhost", 9000, "/")]
    [InlineData("http://192.168.1.5:8080/inference", "192.168.1.5", 8080, "/inference")]
    public void TryParseUrl_ParsesValidUrls(string url, string expectedHost, int expectedPort, string expectedPath)
    {
        var ok = WhisperClient.TryParseUrl(url, out var host, out var port, out var path);
        Assert.True(ok);
        Assert.Equal(expectedHost, host);
        Assert.Equal(expectedPort, port);
        Assert.Equal(expectedPath, path);
    }

    [Theory]
    [InlineData("https://localhost:9000/path")]  // wrong scheme
    [InlineData("http://localhost:abc/path")]    // non-numeric port
    [InlineData("ftp://localhost:9000/path")]
    public void TryParseUrl_RejectsInvalidUrls(string url)
    {
        var ok = WhisperClient.TryParseUrl(url, out _, out _, out _);
        Assert.False(ok);
    }

    [Fact]
    public void ExtractText_ReadsSimpleTextField()
    {
        var text = WhisperClient.ExtractText("{\"text\":\"Roger, copy\"}");
        Assert.Equal("Roger, copy", text);
    }

    [Fact]
    public void ExtractText_HandlesEscapedQuotes()
    {
        // The old substring-based parser broke on exactly this case.
        var text = WhisperClient.ExtractText("{\"text\":\"He said \\\"hello\\\" to the pilot\"}");
        Assert.Equal("He said \"hello\" to the pilot", text);
    }

    [Fact]
    public void ExtractText_HandlesUnicode()
    {
        var text = WhisperClient.ExtractText("{\"text\":\"caf\\u00e9\"}");
        Assert.Equal("café", text);
    }

    [Fact]
    public void ExtractText_TrimsWhitespace()
    {
        var text = WhisperClient.ExtractText("{\"text\":\"  hello  \"}");
        Assert.Equal("hello", text);
    }

    [Fact]
    public void ExtractText_MissingField_ReturnsEmpty()
    {
        var text = WhisperClient.ExtractText("{\"other\":\"value\"}");
        Assert.Equal("", text);
    }

    [Fact]
    public void ExtractText_MalformedJson_ReturnsEmptyWithoutThrowing()
    {
        var text = WhisperClient.ExtractText("not json at all");
        Assert.Equal("", text);
    }
}
