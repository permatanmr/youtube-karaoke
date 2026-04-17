# YouTube Split Downloader

A simple Python app that:
- downloads a YouTube video URL
- exports audio-only file
- separates person voice (vocals) and music (instrumental) into separate files
- exports video-only file (without sound)
- generates subtitle `.srt` from available YouTube auto-transcription
- falls back to OpenAI transcription from vocals audio when YouTube subtitles are unavailable
- restricts download to videos with maximum duration of 5 minutes

## Requirements

- Python 3.10+
- FFmpeg installed and available in PATH

On macOS (Homebrew):

```bash
brew install ffmpeg
```

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

If you want subtitle fallback through OpenAI, set your API key first:

```bash
export OPENAI_API_KEY="your_api_key_here"
```

You can also place it in a local `.env` file:

```bash
OPENAI_API_KEY=your_api_key_here
```

If you manage Node.js with `nvm`, the app automatically prefers a supported Node version when the shell default is too old for yt-dlp's YouTube challenge solver.

## Usage

```bash
python app.py "https://www.youtube.com/watch?v=YOUR_VIDEO_ID"
```

If you use zsh on macOS, keep the URL quoted. Otherwise `?` is treated as a glob and the shell fails before Python runs.

You can also run the app without a URL argument and paste the link when prompted:

```bash
python app.py
```

Optional arguments:

- `-o, --output-dir` output folder (default: `outputs`)
- `--audio-format` one of `mp3`, `m4a`, `wav`, `aac`, `opus`
- `--video-format` one of `mp4`, `mkv`, `webm`
- `--cookies` path to a Netscape-format cookies file
- `--cookies-from-browser` browser cookie source, for example `chrome` or `safari`
- `--js-runtime` JavaScript runtime for yt-dlp, for example `node`
- `--subtitle-langs` subtitle language priority, comma-separated (default: `id.*,en.*`)
- `--openai-model` OpenAI model for subtitle fallback (default: `whisper-1`)
- `--openai-language` optional language hint for OpenAI transcription
- `--burn-subtitles` burn subtitle text into video frames (hard subtitles)

Example:

```bash
python app.py "https://www.youtube.com/watch?v=YOUR_VIDEO_ID" -o downloads --audio-format wav --video-format mkv
```

If YouTube returns a bot-check or sign-in challenge, use one of these:

```bash
python app.py "https://www.youtube.com/watch?v=YOUR_VIDEO_ID" --cookies-from-browser chrome
```

```bash
python app.py "https://www.youtube.com/watch?v=YOUR_VIDEO_ID" --cookies-from-browser safari
```

```bash
python app.py "https://www.youtube.com/watch?v=YOUR_VIDEO_ID" --cookies ~/Downloads/cookies.txt
```

## Output

Files are saved in a timestamped subfolder inside the output directory:
`outputs/<video_title>_YYYYMMDD_HHMMSS/`

Generated files in the run folder:
- `<title>_audio.<audio-format>`
- `<title>_vocals.wav`
- `<title>_music.wav`
- `<title>_video_muted.<video-format>`
- `<title>_video_muted.srt`
- `<title>_karaoke.mp4` (muted video + music + subtitle in one file)
- `<title>_karaoke.srt` (sidecar subtitle with same basename as karaoke video)

## Notes

- Use this tool only for content you are allowed to download and process.
- If FFmpeg is not installed, conversion/extraction will fail.
- Stem separation uses Demucs and runs after audio download.
- Subtitle export uses YouTube subtitles/auto-subtitles and writes an `.srt` file with the same basename as the muted video.
- If YouTube subtitles are unavailable, the app transcribes the separated vocals track with OpenAI and writes the same `.srt` output.
- Subtitle timing is automatically shifted 1 second earlier to improve sync for karaoke playback.
- The app creates a final karaoke `.mp4` with embedded subtitle track and also writes `<title>_karaoke.srt` sidecar for players that prefer external subtitles.
- If `--burn-subtitles` is used, the app attempts to hard-burn subtitle text into video frames. If FFmpeg lacks subtitle filter support, it falls back to embedded + sidecar subtitles.
- Videos longer than 5 minutes are blocked before download starts.
- The app auto-detects `node`, `bun`, `quickjs`, or `deno` for yt-dlp JavaScript execution.
- For YouTube extraction, the bundled auto-detection prefers Node.js `20+` when multiple `nvm` versions are installed.
- On macOS, `--cookies-from-browser safari` or `--cookies-from-browser chrome` can help with YouTube bot checks if you are signed in there.


TESTING 
`./.venv/bin/python app.py "https://www.youtube.com/watch?v=UX8aaMDElJg" --cookies-from-browser chrome --subtitle-langs "id"`