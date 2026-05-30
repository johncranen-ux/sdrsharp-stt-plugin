using System.Windows.Forms;
using SDRSharp.Common;
using SDRSharp.Radio;

namespace SDRSharp.SttPlugin
{
    public class SttPlugin : ISharpPlugin, ICanLazyLoadGui, IExtendedNameProvider, ISupportStatus
    {
        private ISharpControl? _control;
        private ControlPanel?  _gui;
        private AudioProcessor? _audioProcessor;
        private WhisperClient?  _whisperClient;

        public string DisplayName => "Speech to Text";
        public string Category    => "Audio";
        public string MenuItemName => DisplayName;
        public bool   IsActive    => _gui != null && _gui.Visible;

        public UserControl Gui
        {
            get { LoadGui(); return _gui!; }
        }

        public void LoadGui()
        {
            if (_gui == null)
                _gui = new ControlPanel(_audioProcessor!, _whisperClient!);
        }

        public void Initialize(ISharpControl control)
        {
            _control       = control;
            _whisperClient = new WhisperClient();
            _audioProcessor = new AudioProcessor(_whisperClient, control);
            _control.RegisterStreamHook(_audioProcessor, ProcessorType.FilteredAudioOutput);
        }

        public void Close()
        {
            _gui?.SaveSettings();

            if (_control != null && _audioProcessor != null)
                _control.UnregisterStreamHook(_audioProcessor);

            _audioProcessor?.Dispose();
            _whisperClient?.Dispose();
        }
    }
}
