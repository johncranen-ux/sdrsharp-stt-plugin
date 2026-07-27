using System;
using System.IO;
using System.Xml;

namespace SDRSharp.SttPlugin
{
    // Persists plugin settings to an XML file next to the plugin DLL.
    internal static class PluginSettings
    {
        private static readonly string SettingsFile = Path.Combine(
            Path.GetDirectoryName(typeof(PluginSettings).Assembly.Location)
            ?? AppContext.BaseDirectory,
            "SDRSharp.SttPlugin.xml");

        public static string  ServerUrl { get; set; } = "http://localhost:9000";
        public static string  Mode      { get; set; } = "maritime";
        public static string  Language  { get; set; } = "en";
        // Whisper's initial prompt biases decoding style/vocabulary from fluent example
        // text, not from a keyword list — a dense list of terms tends to get echoed back
        // verbatim on noisy or silent audio instead of improving real transcriptions.
        public static string  Prompt    { get; set; } =
            "Maas Approach, this is Motortanker Neptune, callsign PABC, requesting permission " +
            "to enter the Botlek, over. " +
            "Motortanker Neptune, Maas Approach, roger, proceed to VHF channel six one, out. " +
            "Rotterdam VTS, be advised we are standing by on channel one six, over.";
        public static int     VadLevel  { get; set; } = 10;
        public static int     SilenceMs { get; set; } = 600;
        public static bool    Enabled   { get; set; } = false;
        public static bool    CaptureChunks     { get; set; } = false;
        public static bool    CaptureContinuous { get; set; } = false;

        public static void Load()
        {
            try
            {
                if (!File.Exists(SettingsFile)) return;
                var doc = new XmlDocument();
                doc.Load(SettingsFile);
                var root = doc.DocumentElement;
                if (root == null) return;

                ServerUrl = GetValue(root, "ServerUrl", ServerUrl);
                Mode      = GetValue(root, "Mode",      Mode);
                Language  = GetValue(root, "Language",  Language);
                Prompt    = GetValue(root, "Prompt",    Prompt);
                VadLevel  = GetInt(root,  "VadLevel",  VadLevel);
                SilenceMs = GetInt(root,  "SilenceMs", SilenceMs);
                Enabled   = GetBool(root, "Enabled",   Enabled);
                CaptureChunks     = GetBool(root, "CaptureChunks",     CaptureChunks);
                CaptureContinuous = GetBool(root, "CaptureContinuous", CaptureContinuous);
            }
            catch { }
        }

        public static void Save()
        {
            try
            {
                var doc  = new XmlDocument();
                var root = doc.CreateElement("SttPluginSettings");
                doc.AppendChild(root);

                SetValue(doc, root, "ServerUrl", ServerUrl);
                SetValue(doc, root, "Mode",      Mode);
                SetValue(doc, root, "Language",  Language);
                SetValue(doc, root, "Prompt",    Prompt);
                SetValue(doc, root, "VadLevel",  VadLevel.ToString());
                SetValue(doc, root, "SilenceMs", SilenceMs.ToString());
                SetValue(doc, root, "Enabled",   Enabled.ToString());
                SetValue(doc, root, "CaptureChunks",     CaptureChunks.ToString());
                SetValue(doc, root, "CaptureContinuous", CaptureContinuous.ToString());

                doc.Save(SettingsFile);
            }
            catch { }
        }

        private static string GetValue(XmlElement root, string name, string fallback)
        {
            var node = root.SelectSingleNode(name);
            return node?.InnerText ?? fallback;
        }

        private static int GetInt(XmlElement root, string name, int fallback)
        {
            var node = root.SelectSingleNode(name);
            return node != null && int.TryParse(node.InnerText, out int v) ? v : fallback;
        }

        private static bool GetBool(XmlElement root, string name, bool fallback)
        {
            var node = root.SelectSingleNode(name);
            return node != null && bool.TryParse(node.InnerText, out bool v) ? v : fallback;
        }

        private static void SetValue(XmlDocument doc, XmlElement root, string name, string value)
        {
            var node = doc.CreateElement(name);
            node.InnerText = value;
            root.AppendChild(node);
        }
    }
}
