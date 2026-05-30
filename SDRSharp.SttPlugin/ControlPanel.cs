using System;
using System.Windows.Forms;
using System.Timers;

namespace SDRSharp.SttPlugin
{
    public partial class ControlPanel : UserControl
    {
        private readonly AudioProcessor _audioProcessor;
        private readonly WhisperClient  _whisperClient;
        private readonly System.Timers.Timer _diagTimer;

        public ControlPanel(AudioProcessor audioProcessor, WhisperClient whisperClient)
        {
            _audioProcessor = audioProcessor;
            _whisperClient  = whisperClient;

            InitializeComponent();

            // Load persisted settings then apply to components.
            PluginSettings.Load();
            ApplySettings();

            _whisperClient.StatusChanged += OnTranscriptReceived;

            _diagTimer = new System.Timers.Timer(1000) { AutoReset = true };
            _diagTimer.Elapsed += (_, _) =>
            {
                if (!IsDisposed)
                    UpdateStatusLabel(_audioProcessor.ConsumerState);
            };
            _diagTimer.Start();
        }

        private void ApplySettings()
        {
            txtServerUrl.Text  = PluginSettings.ServerUrl;
            txtLanguage.Text   = PluginSettings.Language;
            txtPrompt.Text     = PluginSettings.Prompt;
            nudVadLevel.Value  = PluginSettings.VadLevel;
            nudSilenceMs.Value = PluginSettings.SilenceMs;
            chkEnable.Checked  = PluginSettings.Enabled;

            int modeIdx = cmbMode.Items.IndexOf(
                System.Globalization.CultureInfo.CurrentCulture.TextInfo
                      .ToTitleCase(PluginSettings.Mode));
            cmbMode.SelectedIndex = modeIdx >= 0 ? modeIdx : 0;

            // Push loaded settings into the client/processor.
            _whisperClient.ServerUrl = PluginSettings.ServerUrl;
            _whisperClient.Language  = PluginSettings.Language;
            _whisperClient.Prompt    = PluginSettings.Prompt;
            _whisperClient.Mode      = PluginSettings.Mode;
            _audioProcessor.VadLevel  = PluginSettings.VadLevel;
            _audioProcessor.SilenceMs = PluginSettings.SilenceMs;
            _audioProcessor.Enabled   = PluginSettings.Enabled;
        }

        public void SaveSettings()
        {
            PluginSettings.ServerUrl = _whisperClient.ServerUrl;
            PluginSettings.Mode      = _whisperClient.Mode;
            PluginSettings.Language  = _whisperClient.Language;
            PluginSettings.Prompt    = _whisperClient.Prompt;
            PluginSettings.VadLevel  = _audioProcessor.VadLevel;
            PluginSettings.SilenceMs = _audioProcessor.SilenceMs;
            PluginSettings.Enabled   = _audioProcessor.Enabled;
            PluginSettings.Save();
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                _diagTimer.Stop();
                _diagTimer.Dispose();
                _whisperClient.StatusChanged -= OnTranscriptReceived;
                components?.Dispose();
            }
            base.Dispose(disposing);
        }

        // ── Event handlers ────────────────────────────────────────────────

        private void chkEnable_CheckedChanged(object sender, EventArgs e)
        {
            _audioProcessor.Enabled = chkEnable.Checked;
        }

        private void cmbMode_SelectionChangeCommitted(object sender, EventArgs e)
        {
            _whisperClient.Mode = cmbMode.Text.ToLowerInvariant();
        }

        private void txtServerUrl_Leave(object sender, EventArgs e)
        {
            _whisperClient.ServerUrl = txtServerUrl.Text.Trim();
        }

        private void txtServerUrl_KeyDown(object sender, KeyEventArgs e)
        {
            if (e.KeyCode == Keys.Enter)
                _whisperClient.ServerUrl = txtServerUrl.Text.Trim();
        }

        private void nudVadLevel_ValueChanged(object sender, EventArgs e)
        {
            _audioProcessor.VadLevel = (int)nudVadLevel.Value;
        }

        private void nudSilenceMs_ValueChanged(object sender, EventArgs e)
        {
            _audioProcessor.SilenceMs = (int)nudSilenceMs.Value;
        }

        private void txtLanguage_Leave(object sender, EventArgs e)
        {
            _whisperClient.Language = txtLanguage.Text.Trim();
        }

        private void txtLanguage_KeyDown(object sender, KeyEventArgs e)
        {
            if (e.KeyCode == Keys.Enter)
                _whisperClient.Language = txtLanguage.Text.Trim();
        }

        private void txtPrompt_Leave(object sender, EventArgs e)
        {
            _whisperClient.Prompt = txtPrompt.Text.Trim();
        }

        private void txtPrompt_KeyDown(object sender, KeyEventArgs e)
        {
            if (e.KeyCode == Keys.Enter)
                _whisperClient.Prompt = txtPrompt.Text.Trim();
        }

        private void btnClearTranscript_Click(object sender, EventArgs e)
        {
            txtTranscript.Clear();
        }

        private void btnSaveSettings_Click(object sender, EventArgs e)
        {
            SaveSettings();
            lblStatus.Text = "Settings saved.";
        }

        private void btnDebugSave_Click(object sender, EventArgs e)
        {
            _audioProcessor.DebugSaveNext = true;
            lblStatus.Text = "Next chunk will be saved as debug_chunk.wav";
        }

        // ── Status / transcript updates ───────────────────────────────────

        private const int MaxTranscriptLines = 500;

        private void OnTranscriptReceived(string text)
        {
            if (IsDisposed) return;
            if (InvokeRequired)
                BeginInvoke(new Action(() => AppendTranscript(text)));
            else
                AppendTranscript(text);
        }

        private void UpdateStatusLabel(string status)
        {
            if (IsDisposed) return;
            if (InvokeRequired)
                BeginInvoke(new Action(() => lblStatus.Text = status));
            else
                lblStatus.Text = status;
        }

        private void AppendTranscript(string text)
        {
            if (IsDisposed) return;
            var line = $"[{DateTime.Now:HH:mm:ss}] {text}";
            if (txtTranscript.TextLength > 0)
                txtTranscript.AppendText(Environment.NewLine);
            txtTranscript.AppendText(line);

            if (txtTranscript.Lines.Length > MaxTranscriptLines)
                txtTranscript.Lines = txtTranscript.Lines[^MaxTranscriptLines..];

            txtTranscript.ScrollToCaret();
        }
    }
}
