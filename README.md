# Local Audio/Video Transcription

This project is designed to transcribe local files and YouTube URLs using `faster-whisper`.

## Hugging Face Token (Recommended)

To avoid unauthenticated download warnings and improve model download limits/speed:

```powershell
.\set-hf-token.ps1
```

Or create a local `.env` file (see `.env.example`) with:

```text
HF_TOKEN=hf_...
HUGGINGFACE_HUB_TOKEN=hf_...
```

`launch-ui.ps1`, `transcribe.ps1`, and `download-audio.ps1` now load `.env` automatically.

## Local Interface

Start the app:

```bat
abrir-cabina.bat
```

or if you prefer PowerShell:

```powershell
.\launch-ui.ps1
```

The interface will open at:

```text
http://127.0.0.1:7860
```

Stop the app:

```bat
cerrar-cabina.bat
```

or:

```powershell
.\stop-ui.ps1
```

From the interface, you can:

- Upload `mp3`, `wav`, `m4a`, `mp4`, `mkv` files.
- Paste a YouTube URL.
- Choose the model, language, and computing device.
- Download results in `txt`, `srt`, `vtt`, `json`, `summary.md`, and `run.log` formats.

The app processes one transcription at a time. Extra jobs are queued automatically so a `large-v3`
run does not compete with another transcription for GPU/CPU memory. Terminal runs use the same
local lock, so launching two scripts at once will make the second one wait for its turn.

## Terminal Usage

Transcribe a local file:

```powershell
.\transcribe.ps1 -FilePath "C:\path\to\audio.mp3"
```

Queue a local file in the background:

```powershell
.\queue-transcription.ps1 -FilePath "C:\path\to\audio.mp3"
```

Queued terminal jobs share the same local lock as the UI. Each background job writes
launcher logs under `logs\queued-*`; transcription outputs still go to `transcripts`.

Download only the audio from YouTube:

```powershell
.\download-audio.ps1 -Url "https://www.youtube.com/watch?v=VIDEO_ID"
```

Then transcribe the downloaded file:

```powershell
.\transcribe.ps1 -FilePath ".\downloads\filename.m4a"
```

## Useful Options

Force CPU:

```powershell
.\transcribe.ps1 -FilePath "C:\path\to\video.mp4" -Cpu
```

Use a faster model:

```powershell
.\transcribe.ps1 -FilePath "C:\path\to\audio.mp3" -Model "turbo"
```

Let it detect the language automatically:

```powershell
.\transcribe.ps1 -FilePath "C:\path\to\audio.mp3" -Language "auto"
```

## Notes

- `large-v3` prioritizes quality.
- `turbo` is significantly faster.
- The first time a new model is used, it will take longer as it downloads the weights.
- The pipeline uses GPU automatically if available.
- Text subtitle outputs are written with UTF-8 BOM for better compatibility with Windows tools.
- Each output folder includes `run.log` with live progress events and `summary.md` with an automatic extractive summary.

## Privacy and Public Sharing

- Keep tokens only in local `.env` files.
- Never commit `.env` or private transcription outputs.
- Use `.env.example` only as a template with placeholder values.
