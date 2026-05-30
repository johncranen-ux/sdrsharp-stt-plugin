using System.Windows.Forms;

namespace SDRSharp.SttPlugin
{
    partial class ControlPanel
    {
        private System.ComponentModel.IContainer components = null;

        private Label         lblServerUrl;
        private TextBox       txtServerUrl;
        private Label         lblMode;
        private ComboBox      cmbMode;
        private Label         lblVadLevel;
        private NumericUpDown nudVadLevel;
        private Label         lblSilenceMs;
        private NumericUpDown nudSilenceMs;
        private Label         lblLanguage;
        private TextBox       txtLanguage;
        private Label         lblPrompt;
        private TextBox       txtPrompt;
        private CheckBox      chkEnable;
        private Label         lblStatus;
        private Label         lblTranscriptHeader;
        private Button        btnClearTranscript;
        private Button        btnSaveSettings;
        private Button        btnDebugSave;
        private TextBox       txtTranscript;
        private ToolTip       toolTip;

        private void InitializeComponent()
        {
            components          = new System.ComponentModel.Container();
            toolTip             = new ToolTip(components);

            lblServerUrl        = new Label();
            txtServerUrl        = new TextBox();
            lblMode             = new Label();
            cmbMode             = new ComboBox();
            lblVadLevel         = new Label();
            nudVadLevel         = new NumericUpDown();
            lblSilenceMs        = new Label();
            nudSilenceMs        = new NumericUpDown();
            lblLanguage         = new Label();
            txtLanguage         = new TextBox();
            lblPrompt           = new Label();
            txtPrompt           = new TextBox();
            chkEnable           = new CheckBox();
            lblStatus           = new Label();
            lblTranscriptHeader = new Label();
            btnClearTranscript  = new Button();
            btnSaveSettings     = new Button();
            btnDebugSave        = new Button();
            txtTranscript       = new TextBox();

            ((System.ComponentModel.ISupportInitialize)nudVadLevel).BeginInit();
            ((System.ComponentModel.ISupportInitialize)nudSilenceMs).BeginInit();
            SuspendLayout();

            // ── lblServerUrl ─────────────────────────────────────────────
            lblServerUrl.AutoSize = true;
            lblServerUrl.Location = new System.Drawing.Point(6, 4);
            lblServerUrl.Text     = "Server URL:";

            // ── txtServerUrl ─────────────────────────────────────────────
            txtServerUrl.Location = new System.Drawing.Point(6, 20);
            txtServerUrl.Size     = new System.Drawing.Size(288, 23);
            txtServerUrl.TabIndex = 0;
            txtServerUrl.Leave   += new System.EventHandler(txtServerUrl_Leave);
            txtServerUrl.KeyDown += new KeyEventHandler(txtServerUrl_KeyDown);
            toolTip.SetToolTip(txtServerUrl, "URL of the Whisper proxy, e.g. http://localhost:9000");

            // ── lblMode ──────────────────────────────────────────────────
            lblMode.AutoSize = true;
            lblMode.Location = new System.Drawing.Point(6, 48);
            lblMode.Text     = "Mode:";

            // ── cmbMode ──────────────────────────────────────────────────
            cmbMode.Location      = new System.Drawing.Point(6, 64);
            cmbMode.Size          = new System.Drawing.Size(120, 23);
            cmbMode.TabIndex      = 1;
            cmbMode.DropDownStyle = ComboBoxStyle.DropDownList;
            cmbMode.Items.AddRange(new object[] { "Maritime", "Airband" });
            cmbMode.SelectedIndex = 0;
            cmbMode.SelectionChangeCommitted += new System.EventHandler(cmbMode_SelectionChangeCommitted);
            toolTip.SetToolTip(cmbMode, "Maritime: vessel extraction + AIS. Airband: speech-to-text only.");

            // ── lblVadLevel ───────────────────────────────────────────────
            lblVadLevel.AutoSize = true;
            lblVadLevel.Location = new System.Drawing.Point(6, 92);
            lblVadLevel.Text     = "VAD threshold:";

            // ── nudVadLevel ───────────────────────────────────────────────
            nudVadLevel.Location     = new System.Drawing.Point(6, 108);
            nudVadLevel.Minimum      = 0;
            nudVadLevel.Maximum      = 100;
            nudVadLevel.Value        = 10;
            nudVadLevel.Size         = new System.Drawing.Size(55, 23);
            nudVadLevel.TabIndex     = 2;
            nudVadLevel.ValueChanged += new System.EventHandler(nudVadLevel_ValueChanged);
            toolTip.SetToolTip(nudVadLevel, "RMS threshold 0-100 (0=disabled). Frames below this are silence.");

            // ── lblSilenceMs ──────────────────────────────────────────────
            lblSilenceMs.AutoSize = true;
            lblSilenceMs.Location = new System.Drawing.Point(72, 92);
            lblSilenceMs.Text     = "End silence (ms):";

            // ── nudSilenceMs ──────────────────────────────────────────────
            nudSilenceMs.Location     = new System.Drawing.Point(72, 108);
            nudSilenceMs.Minimum      = 100;
            nudSilenceMs.Maximum      = 3000;
            nudSilenceMs.Increment    = 100;
            nudSilenceMs.Value        = 600;
            nudSilenceMs.Size         = new System.Drawing.Size(60, 23);
            nudSilenceMs.TabIndex     = 3;
            nudSilenceMs.ValueChanged += new System.EventHandler(nudSilenceMs_ValueChanged);
            toolTip.SetToolTip(nudSilenceMs, "Milliseconds of trailing silence before a speech chunk is sent.");

            // ── lblLanguage ───────────────────────────────────────────────
            lblLanguage.AutoSize = true;
            lblLanguage.Location = new System.Drawing.Point(6, 136);
            lblLanguage.Text     = "Language:";

            // ── txtLanguage ───────────────────────────────────────────────
            txtLanguage.Location = new System.Drawing.Point(6, 152);
            txtLanguage.Size     = new System.Drawing.Size(50, 23);
            txtLanguage.TabIndex = 4;
            txtLanguage.Leave   += new System.EventHandler(txtLanguage_Leave);
            txtLanguage.KeyDown += new KeyEventHandler(txtLanguage_KeyDown);
            toolTip.SetToolTip(txtLanguage, "ISO language code, e.g. en (blank = auto-detect)");

            // ── lblPrompt ─────────────────────────────────────────────────
            lblPrompt.AutoSize = true;
            lblPrompt.Location = new System.Drawing.Point(66, 136);
            lblPrompt.Text     = "Initial prompt:";

            // ── txtPrompt ─────────────────────────────────────────────────
            txtPrompt.Location = new System.Drawing.Point(66, 152);
            txtPrompt.Size     = new System.Drawing.Size(228, 23);
            txtPrompt.TabIndex = 5;
            txtPrompt.Leave   += new System.EventHandler(txtPrompt_Leave);
            txtPrompt.KeyDown += new KeyEventHandler(txtPrompt_KeyDown);
            toolTip.SetToolTip(txtPrompt, "Hint text to bias recognition, e.g. 'Maas Approach Rotterdam VTS'");

            // ── chkEnable ────────────────────────────────────────────────
            chkEnable.AutoSize        = true;
            chkEnable.Location        = new System.Drawing.Point(6, 182);
            chkEnable.Text            = "Enable transcription";
            chkEnable.TabIndex        = 6;
            chkEnable.CheckedChanged += new System.EventHandler(chkEnable_CheckedChanged);

            // ── btnSaveSettings ───────────────────────────────────────────
            btnSaveSettings.Location  = new System.Drawing.Point(158, 178);
            btnSaveSettings.Size      = new System.Drawing.Size(65, 22);
            btnSaveSettings.Text      = "Save";
            btnSaveSettings.TabIndex  = 7;
            btnSaveSettings.Click    += new System.EventHandler(btnSaveSettings_Click);
            toolTip.SetToolTip(btnSaveSettings, "Save current settings to disk");

            // ── btnDebugSave ──────────────────────────────────────────────
            btnDebugSave.Location  = new System.Drawing.Point(228, 178);
            btnDebugSave.Size      = new System.Drawing.Size(66, 22);
            btnDebugSave.Text      = "Debug WAV";
            btnDebugSave.TabIndex  = 8;
            btnDebugSave.Click    += new System.EventHandler(btnDebugSave_Click);
            toolTip.SetToolTip(btnDebugSave, "Save next audio chunk to debug_chunk.wav for inspection");

            // ── lblStatus ────────────────────────────────────────────────
            lblStatus.AutoSize  = false;
            lblStatus.Location  = new System.Drawing.Point(6, 206);
            lblStatus.Size      = new System.Drawing.Size(288, 16);
            lblStatus.Text      = "Idle";
            lblStatus.ForeColor = System.Drawing.SystemColors.GrayText;
            lblStatus.Font      = new System.Drawing.Font("Segoe UI", 7.5f);

            // ── lblTranscriptHeader ───────────────────────────────────────
            lblTranscriptHeader.AutoSize = true;
            lblTranscriptHeader.Location = new System.Drawing.Point(6, 226);
            lblTranscriptHeader.Text     = "Transcript:";

            // ── btnClearTranscript ────────────────────────────────────────
            btnClearTranscript.Location  = new System.Drawing.Point(246, 222);
            btnClearTranscript.Size      = new System.Drawing.Size(48, 20);
            btnClearTranscript.Text      = "Clear";
            btnClearTranscript.TabIndex  = 9;
            btnClearTranscript.Click    += new System.EventHandler(btnClearTranscript_Click);

            // ── txtTranscript ─────────────────────────────────────────────
            txtTranscript.Location   = new System.Drawing.Point(6, 244);
            txtTranscript.Size       = new System.Drawing.Size(288, 270);
            txtTranscript.Multiline  = true;
            txtTranscript.ReadOnly   = true;
            txtTranscript.ScrollBars = ScrollBars.Vertical;
            txtTranscript.BackColor  = System.Drawing.SystemColors.Window;
            txtTranscript.TabStop    = false;
            txtTranscript.Font       = new System.Drawing.Font("Consolas", 8.25f);

            // ── ControlPanel ──────────────────────────────────────────────
            AutoScaleDimensions = new System.Drawing.SizeF(7F, 15F);
            AutoScaleMode       = AutoScaleMode.Font;
            Controls.Add(lblServerUrl);
            Controls.Add(txtServerUrl);
            Controls.Add(lblMode);
            Controls.Add(cmbMode);
            Controls.Add(lblVadLevel);
            Controls.Add(nudVadLevel);
            Controls.Add(lblSilenceMs);
            Controls.Add(nudSilenceMs);
            Controls.Add(lblLanguage);
            Controls.Add(txtLanguage);
            Controls.Add(lblPrompt);
            Controls.Add(txtPrompt);
            Controls.Add(chkEnable);
            Controls.Add(btnSaveSettings);
            Controls.Add(btnDebugSave);
            Controls.Add(lblStatus);
            Controls.Add(lblTranscriptHeader);
            Controls.Add(btnClearTranscript);
            Controls.Add(txtTranscript);
            Name        = "ControlPanel";
            Size        = new System.Drawing.Size(300, 520);
            MinimumSize = new System.Drawing.Size(300, 520);
            TabIndex    = 0;

            ((System.ComponentModel.ISupportInitialize)nudVadLevel).EndInit();
            ((System.ComponentModel.ISupportInitialize)nudSilenceMs).EndInit();
            ResumeLayout(false);
            PerformLayout();
        }
    }
}
