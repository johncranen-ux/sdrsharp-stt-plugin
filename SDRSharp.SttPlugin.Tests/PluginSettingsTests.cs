namespace SDRSharp.SttPlugin.Tests;

public class PluginSettingsTests
{
    // The decoder prompt is proxy-owned (see server/stt_proxy/backends.py: the effective
    // prompt is `client_prompt or DEFAULT_MARITIME_PROMPT`, so ANY non-empty prompt from
    // the plugin shadows the server's). A built-in default here silently pins every
    // deployment to whatever prompt shipped with the DLL, which is exactly what happened:
    // the plugin kept sending a prompt naming "Motortanker Neptune" long after the server
    // moved to a measured replacement, so the better prompt never ran in production and a
    // phantom vessel name -- one that matches a real AIS entry at 100 -- kept being echoed
    // into transcripts. Empty means "let the server decide"; the textbox stays available
    // as a deliberate per-site override.
    [Fact]
    public void Prompt_DefaultsToEmpty_SoTheProxyOwnsIt()
    {
        Assert.Equal(string.Empty, PluginSettings.Prompt);
    }
}
