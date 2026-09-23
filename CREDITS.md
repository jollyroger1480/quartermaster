# Credits & Providers

Quartermaster stands on the shoulders of a lot of excellent free and open
work — plus a few cloud services. Full credit where it's due:

## Voice

| Component | Role | Thanks to |
|---|---|---|
| [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) | primary TTS voice (Apache-2.0) | hexgrad |
| [misaki](https://github.com/hexgrad/misaki) / [spacy en_core_web_sm](https://github.com/explosion/spacy-models) | G2P for Kokoro | hexgrad / Explosion |
| [piper](https://github.com/rhasspy/piper) | fallback TTS (MIT) | the Rhasspy project |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (Whisper) | speech-to-text (MIT) | SYSTRAN / OpenAI Whisper |

## Phone / audio plumbing

| Component | Role | Thanks to |
|---|---|---|
| [bluez-alsa](https://github.com/arkq/bluez-alsa) | the HFP daemon that makes phone-call audio on Linux actually work (MIT) | Arkq & contributors |
| [BlueZ](http://www.bluez.org/) | Bluetooth stack | BlueZ contributors |
| [PipeWire](https://pipewire.org/) / [WirePlumber](https://gitlab.freedesktop.org/pipewire/wireplumber) | A2DP audio + system audio graph | Wim Taymans & contributors |
| Android Debug Bridge | the entire control channel (answer, hang up, call state, SMS database) | Android Open Source Project |

## Brains (LLM providers)

The bot speaks to any OpenAI-compatible chat-completions endpoint. Providers
are configured in `callscoot.toml` and tried top to bottom:

- **[Z.ai — GLM](https://z.ai/)** — primary brain during development (GLM-5.3-Flash)
- **[Groq](https://groq.com/)** — free-tier fallback, blisteringly fast
- **[NVIDIA NIM](https://build.nvidia.com/)** — free-tier fallback
- **[Ollama](https://ollama.com/)** — fully local option, no cloud at all
- **[FreeLLM / Headroom](https://github.com/1rgs/FreeLLM)** — free-tier aggregator used during development

Your keys, your environment, your choice. The bot ships provider-agnostic.

## Alerts

- **Telegram Bot API** — call/text digests pushed to the owner's phone

## Special thanks

- Every salvager who ever missed a call while elbow-deep in an engine bay.
- The r/PipeWire threads that document, sentence by sentence, the exact
  misery of Bluetooth HFP on Linux — and the light at the end.

## License

MIT — see [LICENSE](LICENSE). Free as in freedom, free as in beer.
If it made ye coin, the tip jar be in the README. 🏴‍☠️
