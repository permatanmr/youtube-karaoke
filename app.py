#!/usr/bin/env python3
"""Download a YouTube video and export audio-only + video-only files."""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

MAX_DURATION_SECONDS = 7 * 60
SUBTITLE_ADVANCE_SECONDS = 2.0
OPENAI_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MIN_SUPPORTED_NODE_VERSION = (20, 0, 0)
SUPPORTED_BROWSER_NAMES = {
    "brave",
    "chrome",
    "chromium",
    "edge",
    "firefox",
    "opera",
    "safari",
    "vivaldi",
    "whale",
}


def load_env_file(env_path: Path) -> None:
    if not env_path.exists() or not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue

        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {'"', "'"}
        ):
            value = value[1:-1]

        os.environ[key] = value

try:
    from yt_dlp import YoutubeDL  # type: ignore[import-not-found]
    from yt_dlp.utils import DownloadError  # type: ignore[import-not-found]
except ModuleNotFoundError:
    YoutubeDL = None  # type: ignore[assignment]

    class DownloadError(Exception):
        """Fallback error type when yt-dlp is unavailable."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download a YouTube URL, then generate audio-only and muted video files."
        )
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="YouTube video URL. If omitted, the app prompts for it.",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default="outputs",
        help="Directory to store the generated files (default: outputs)",
    )
    parser.add_argument(
        "--audio-format",
        choices=["mp3", "m4a", "wav", "aac", "opus"],
        default="mp3",
        help="Audio format for the extracted sound (default: mp3)",
    )
    parser.add_argument(
        "--video-format",
        choices=["mp4", "mkv", "webm"],
        default="mp4",
        help="Container format for muted video (default: mp4)",
    )
    parser.add_argument(
        "--cookies",
        help="Path to a Netscape-format cookies.txt file for authenticated downloads",
    )
    parser.add_argument(
        "--cookies-from-browser",
        metavar="BROWSER[+KEYRING][:PROFILE][::CONTAINER]",
        help=(
            "Load cookies directly from a browser profile, for example "
            "chrome or safari"
        ),
    )
    parser.add_argument(
        "--js-runtime",
        action="append",
        metavar="RUNTIME[:PATH]",
        help=(
            "Enable a JavaScript runtime for yt-dlp. Repeatable. If omitted, "
            "the app auto-detects node, bun, quickjs, or deno."
        ),
    )
    parser.add_argument(
        "--subtitle-langs",
        default="id.*,en.*",
        help=(
            "Comma-separated subtitle language priority for auto subtitles "
            "(default: id.*,en.*)"
        ),
    )
    parser.add_argument(
        "--openai-model",
        default="whisper-1",
        help=(
            "OpenAI transcription model used when YouTube subtitles are unavailable "
            "(default: whisper-1)"
        ),
    )
    parser.add_argument(
        "--openai-language",
        help="Optional language hint for OpenAI transcription, for example id or en",
    )
    parser.add_argument(
        "--burn-subtitles",
        action="store_true",
        help=(
            "Burn subtitles into karaoke video frames. Requires FFmpeg subtitles "
            "filter support (libass). Falls back to soft subtitles if unavailable."
        ),
    )
    return parser.parse_args()


def resolve_url(raw_url: str | None) -> str:
    if raw_url:
        return raw_url

    if sys.stdin.isatty():
        try:
            return input("Paste YouTube URL: ").strip()
        except EOFError:
            return ""

    return sys.stdin.read().strip()


def sanitize_title(title: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_ ." else "_" for ch in title)
    safe = safe.strip().replace(" ", "_")
    return safe or "youtube_video"


def parse_browser_spec(spec: str) -> tuple[str, str | None, str | None, str | None]:
    match = re.fullmatch(
        r"""(?x)
        (?P<name>[^+:]+)
        (?:\s*\+\s*(?P<keyring>[^:]+))?
        (?:\s*:\s*(?!:)(?P<profile>.+?))?
        (?:\s*::\s*(?P<container>.+))?
        """,
        spec,
    )
    if match is None:
        raise ValueError(f"Invalid --cookies-from-browser value: {spec}")

    browser_name, keyring, profile, container = match.group(
        "name",
        "keyring",
        "profile",
        "container",
    )
    browser_name = browser_name.lower()
    if browser_name not in SUPPORTED_BROWSER_NAMES:
        supported = ", ".join(sorted(SUPPORTED_BROWSER_NAMES))
        raise ValueError(
            "Unsupported browser for --cookies-from-browser: "
            f"{browser_name}. Supported values: {supported}"
        )

    return browser_name, profile, keyring.upper() if keyring else None, container


def parse_semver(version_text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_text)
    if match is None:
        return None

    return tuple(int(part) for part in match.groups())


def resolve_supported_node_path() -> str | None:
    candidates: list[Path] = []

    path_node = shutil.which("node")
    if path_node:
        candidates.append(Path(path_node))

    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    if nvm_root.exists():
        candidates.extend(sorted(nvm_root.glob("v*/bin/node"), reverse=True))

    best_match: tuple[tuple[int, int, int], str] | None = None
    seen_paths: set[str] = set()
    for candidate in candidates:
        candidate_path = str(candidate)
        if candidate_path in seen_paths:
            continue
        seen_paths.add(candidate_path)

        try:
            completed = subprocess.run(
                [candidate_path, "--version"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            continue

        version = parse_semver(completed.stdout or completed.stderr)
        if version is None or version < MIN_SUPPORTED_NODE_VERSION:
            continue

        if best_match is None or version > best_match[0]:
            best_match = (version, candidate_path)

    if best_match is None:
        return None

    return best_match[1]


def resolve_js_runtimes(runtime_specs: list[str] | None) -> dict[str, dict[str, str]] | None:
    if runtime_specs:
        runtimes: dict[str, dict[str, str]] = {}
        for runtime_spec in runtime_specs:
            runtime_name, separator, runtime_path = runtime_spec.partition(":")
            runtime_name = runtime_name.strip().lower()
            if not runtime_name:
                raise ValueError(f"Invalid --js-runtime value: {runtime_spec}")

            config: dict[str, str] = {}
            if separator and runtime_path.strip():
                config["path"] = runtime_path.strip()
            runtimes[runtime_name] = config
        return runtimes

    supported_node_path = resolve_supported_node_path()
    if supported_node_path:
        return {"node": {"path": supported_node_path}}

    for runtime_name in ("bun", "quickjs", "deno"):
        runtime_path = shutil.which(runtime_name)
        if runtime_path:
            return {runtime_name: {"path": runtime_path}}

    return None


def build_ydl_opts(args: argparse.Namespace, *, quiet: bool, skip_download: bool) -> dict:
    ydl_opts = {
        "quiet": quiet,
        "noplaylist": True,
        "skip_download": skip_download,
    }

    js_runtimes = resolve_js_runtimes(args.js_runtime)
    if js_runtimes is not None:
        ydl_opts["js_runtimes"] = js_runtimes

    if args.cookies:
        ydl_opts["cookiefile"] = str(Path(args.cookies).expanduser())

    if args.cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = parse_browser_spec(args.cookies_from_browser)

    return ydl_opts


def get_video_info(url: str, args: argparse.Namespace) -> dict:
    with YoutubeDL(build_ydl_opts(args, quiet=True, skip_download=True)) as ydl:
        return ydl.extract_info(url, download=False)


def download_audio(url: str, output_base: Path, audio_format: str, args: argparse.Namespace) -> Path:
    ydl_opts = build_ydl_opts(args, quiet=False, skip_download=False)
    ydl_opts.update({
        "format": "bestaudio/best",
        "outtmpl": str(output_base.with_suffix(".%(ext)s")),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": "192",
            }
        ],
    })

    with YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    return output_base.with_suffix(f".{audio_format}")


def download_muted_video(
    url: str,
    output_base: Path,
    video_format: str,
    args: argparse.Namespace,
) -> Path:
    ydl_opts = build_ydl_opts(args, quiet=False, skip_download=False)
    ydl_opts.update({
        "format": "bestvideo",
        "outtmpl": str(output_base.with_suffix(".%(ext)s")),
        "recodevideo": video_format,
    })

    with YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    return output_base.with_suffix(f".{video_format}")


def download_subtitle(
    url: str,
    subtitle_base: Path,
    subtitle_langs: str,
    args: argparse.Namespace,
) -> Path | None:
    output_dir = subtitle_base.parent
    before_srt = set(output_dir.glob(f"{subtitle_base.name}*.srt"))

    requested_langs = [lang.strip() for lang in subtitle_langs.split(",") if lang.strip()]
    if not requested_langs:
        requested_langs = ["id.*", "en.*"]

    ydl_opts = build_ydl_opts(args, quiet=False, skip_download=True)
    ydl_opts.update(
        {
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": requested_langs,
            "subtitlesformat": "srt/best",
            "outtmpl": str(subtitle_base.with_suffix(".%(ext)s")),
        }
    )

    with YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    after_srt = set(output_dir.glob(f"{subtitle_base.name}*.srt"))
    new_srt = sorted(after_srt - before_srt, key=lambda path: path.stat().st_mtime)
    if not new_srt:
        if subtitle_base.with_suffix(".srt").exists():
            return subtitle_base.with_suffix(".srt")
        return None

    subtitle_file = new_srt[-1]
    final_subtitle = subtitle_base.with_suffix(".srt")
    if subtitle_file != final_subtitle:
        if final_subtitle.exists():
            final_subtitle.unlink()
        shutil.move(str(subtitle_file), str(final_subtitle))

    return final_subtitle


def _srt_time_to_millis(time_text: str) -> int:
    hours, minutes, seconds_millis = time_text.split(":")
    seconds, millis = seconds_millis.split(",")
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(seconds) * 1_000
        + int(millis)
    )


def _millis_to_srt_time(total_millis: int) -> str:
    total_millis = max(0, total_millis)
    hours, remainder = divmod(total_millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def remove_music_phrases_from_subtitle(subtitle_file: Path) -> None:
    music_phrase_pattern = re.compile(r"\[(?:\s*musik\s*|\s*music\s*)\]", re.IGNORECASE)
    cleaned_lines: list[str] = []

    for line in subtitle_file.read_text(encoding="utf-8").splitlines():
        cleaned_line = music_phrase_pattern.sub("", line)
        if cleaned_line.strip() == "":
            cleaned_lines.append("")
            continue

        cleaned_lines.append(re.sub(r"\s{2,}", " ", cleaned_line).strip())

    subtitle_file.write_text("\n".join(cleaned_lines) + "\n", encoding="utf-8")


def shift_subtitle_earlier(subtitle_file: Path, advance_seconds: float) -> None:
    shift_millis = int(max(0.0, advance_seconds) * 1000)
    if shift_millis == 0:
        return

    timing_pattern = re.compile(
        r"^(\d{2}:\d{2}:\d{2},\d{3})\s-->\s(\d{2}:\d{2}:\d{2},\d{3})(.*)$"
    )
    updated_lines: list[str] = []

    for line in subtitle_file.read_text(encoding="utf-8").splitlines():
        match = timing_pattern.match(line)
        if not match:
            updated_lines.append(line)
            continue

        start_ms = _srt_time_to_millis(match.group(1))
        end_ms = _srt_time_to_millis(match.group(2))
        new_start_ms = max(0, start_ms - shift_millis)
        new_end_ms = max(new_start_ms, end_ms - shift_millis)
        suffix = match.group(3)
        updated_lines.append(
            (
                f"{_millis_to_srt_time(new_start_ms)} --> "
                f"{_millis_to_srt_time(new_end_ms)}{suffix}"
            )
        )

    subtitle_file.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")


def ensure_openai_available() -> str:
    if importlib.util.find_spec("openai") is None:
        raise RuntimeError(
            "OpenAI SDK is not installed. Run: pip install openai"
        )

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Export your API key before running the app."
        )

    return api_key


def transcribe_subtitle_with_openai(
    audio_file: Path,
    subtitle_base: Path,
    args: argparse.Namespace,
) -> Path:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "FFmpeg is not installed or not in PATH. Install ffmpeg first."
        )

    # OpenAI transcription has a strict upload limit. Compress to a tiny mono MP3
    # first to keep payload size below the cap while preserving speech quality.
    transcription_audio = subtitle_base.with_name(f"{subtitle_base.name}_transcribe.mp3")
    compress_command = [
        "ffmpeg",
        "-y",
        "-i",
        str(audio_file),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "32k",
        str(transcription_audio),
    ]

    try:
        subprocess.run(compress_command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise RuntimeError(f"Failed to prepare audio for OpenAI transcription. {detail}") from exc

    if not transcription_audio.exists():
        raise RuntimeError("Failed to prepare audio for OpenAI transcription.")

    file_size = transcription_audio.stat().st_size
    if file_size > OPENAI_MAX_UPLOAD_BYTES:
        raise RuntimeError(
            "Prepared transcription audio is still too large for OpenAI upload "
            f"({file_size} bytes > {OPENAI_MAX_UPLOAD_BYTES} bytes)."
        )

    api_key = ensure_openai_available()

    from openai import OpenAI  # type: ignore[import-not-found]

    client = OpenAI(api_key=api_key)
    request_args: dict[str, object] = {"model": args.openai_model, "response_format": "srt"}
    if args.openai_language:
        request_args["language"] = args.openai_language

    try:
        with transcription_audio.open("rb") as audio_stream:
            request_args["file"] = audio_stream
            transcript = client.audio.transcriptions.create(**request_args)
    except Exception as exc:
        raise RuntimeError(f"OpenAI transcription failed. {exc}") from exc
    finally:
        if transcription_audio.exists():
            transcription_audio.unlink()

    subtitle_file = subtitle_base.with_suffix(".srt")
    subtitle_text = transcript if isinstance(transcript, str) else str(transcript)
    subtitle_file.write_text(subtitle_text, encoding="utf-8")
    return subtitle_file


def ensure_demucs_available() -> None:
    if importlib.util.find_spec("demucs") is None:
        raise RuntimeError(
            "Demucs is not installed. Run: pip install demucs"
        )

    if importlib.util.find_spec("torchcodec") is None:
        raise RuntimeError(
            "TorchCodec is not installed. Run: pip install torchcodec"
        )


def separate_vocals_and_music(audio_file: Path, output_dir: Path, title: str) -> tuple[Path, Path]:
    ensure_demucs_available()

    demucs_output_dir = output_dir / ".demucs_output"
    command = [
        sys.executable,
        "-m",
        "demucs.separate",
        "--two-stems",
        "vocals",
        "--out",
        str(demucs_output_dir),
        str(audio_file),
    ]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise RuntimeError(f"Failed to separate vocals/music. {detail}") from exc

    stem_dirs = list(demucs_output_dir.glob(f"*/{audio_file.stem}"))
    if not stem_dirs:
        raise RuntimeError(
            "Demucs separation finished but output stems were not found."
        )

    stem_dir = max(stem_dirs, key=lambda path: path.stat().st_mtime)
    vocals_src = stem_dir / "vocals.wav"
    music_src = stem_dir / "no_vocals.wav"

    if not vocals_src.exists() or not music_src.exists():
        raise RuntimeError(
            "Demucs did not produce expected files 'vocals.wav' and 'no_vocals.wav'."
        )

    vocals_file = output_dir / f"{title}_vocals.wav"
    music_file = output_dir / f"{title}_music.wav"
    shutil.copy2(vocals_src, vocals_file)
    shutil.copy2(music_src, music_file)
    shutil.rmtree(demucs_output_dir, ignore_errors=True)

    return vocals_file, music_file


def combine_karaoke_video(
    muted_video_file: Path,
    music_file: Path,
    subtitle_file: Path,
    output_file: Path,
    burn_subtitles: bool,
) -> Path:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "FFmpeg is not installed or not in PATH. Install ffmpeg first."
        )

    if burn_subtitles:
        subtitle_filename = subtitle_file.name
        subtitle_filename = subtitle_filename.replace("\\", "\\\\")
        subtitle_filename = subtitle_filename.replace("'", "\\'")
        subtitle_filename = subtitle_filename.replace(":", "\\:")

        burn_command = [
            "ffmpeg",
            "-y",
            "-i",
            str(muted_video_file),
            "-i",
            str(music_file),
            "-vf",
            f"subtitles=filename='{subtitle_filename}'",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-crf",
            "22",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-shortest",
            str(output_file),
        ]

        try:
            subprocess.run(
                burn_command,
                check=True,
                capture_output=True,
                text=True,
                cwd=str(subtitle_file.parent),
            )
            return output_file
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
            if "Filter not found" in detail or "Error parsing" in detail:
                print(
                    "Warning: failed to burn subtitles with current FFmpeg build. "
                    "Falling back to soft subtitles.",
                    file=sys.stderr,
                )
            else:
                raise RuntimeError(
                    f"Failed to combine karaoke video with burned subtitles. {detail}"
                ) from exc
    
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(muted_video_file),
        "-i",
        str(music_file),
        "-i",
        str(subtitle_file),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-map",
        "2:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-c:s",
        "mov_text",
        "-disposition:s:0",
        "default",
        "-metadata:s:s:0",
        "language=eng",
        "-metadata:s:s:0",
        "title=Karaoke",
        "-movflags",
        "+faststart",
        "-shortest",
        str(output_file),
    ]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise RuntimeError(f"Failed to combine karaoke video. {detail}") from exc

    return output_file


def print_auth_hint(args: argparse.Namespace) -> None:
    if args.cookies or args.cookies_from_browser:
        return

    print(
        "Hint: YouTube is asking for authentication. Retry with "
        "--cookies-from-browser chrome or --cookies-from-browser safari "
        "if you are signed in there, or use --cookies /path/to/cookies.txt.",
        file=sys.stderr,
    )


def main() -> int:
    load_env_file(Path(".env"))
    args = parse_args()

    if YoutubeDL is None:
        print(
            "Error: yt-dlp is not installed. Run: pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    url = resolve_url(args.url)
    if not url:
        print(
            "Error: missing YouTube URL. Pass it as an argument or paste it when prompted.",
            file=sys.stderr,
        )
        return 1

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        info = get_video_info(url, args)
    except ValueError as exc:
        print(f"Error: invalid option. {exc}", file=sys.stderr)
        return 1
    except DownloadError as exc:
        message = str(exc)
        print(f"Error: failed to read video metadata. {message}", file=sys.stderr)
        if "confirm you" in message.lower() and "not a bot" in message.lower():
            print_auth_hint(args)
        return 1

    duration = info.get("duration")
    if not isinstance(duration, (int, float)):
        print(
            "Error: could not determine video duration. Download is restricted.",
            file=sys.stderr,
        )
        return 1

    if duration > MAX_DURATION_SECONDS:
        print(
            (
                "Error: video is too long. Maximum allowed duration is "
                f"5 minutes ({MAX_DURATION_SECONDS} seconds), "
                f"but this video is {int(duration)} seconds."
            ),
            file=sys.stderr,
        )
        return 1

    title = sanitize_title(info.get("title", "youtube_video"))
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = output_dir / f"{title}_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    audio_base = run_dir / f"{title}_audio"
    muted_video_base = run_dir / f"{title}_video_muted"
    subtitle_base = run_dir / f"{title}_video_muted"
    karaoke_file = run_dir / f"{title}_karaoke.mp4"

    try:
        print("Downloading and extracting audio...")
        audio_file = download_audio(url, audio_base, args.audio_format, args)

        print("Separating vocals and music...")
        vocals_file, music_file = separate_vocals_and_music(audio_file, run_dir, title)

        print("Downloading muted video...")
        muted_video_file = download_muted_video(
            url,
            muted_video_base,
            args.video_format,
            args,
        )

        print("Generating subtitle (.srt)...")
        subtitle_file = download_subtitle(
            url,
            subtitle_base,
            args.subtitle_langs,
            args,
        )
        if subtitle_file is None:
            print("No YouTube subtitle found. Transcribing vocals with OpenAI...")
            subtitle_file = transcribe_subtitle_with_openai(
                vocals_file,
                subtitle_base,
                args,
            )

        print("Removing [Musik]/[Music] phrases from subtitle...")
        remove_music_phrases_from_subtitle(subtitle_file)

        print("Advancing subtitle timing by 1 second...")
        shift_subtitle_earlier(subtitle_file, SUBTITLE_ADVANCE_SECONDS)

        print("Combining muted video + music + subtitle...")
        karaoke_file = combine_karaoke_video(
            muted_video_file,
            music_file,
            subtitle_file,
            karaoke_file,
            args.burn_subtitles,
        )
        karaoke_subtitle_file = karaoke_file.with_suffix(".srt")
        shutil.copy2(subtitle_file, karaoke_subtitle_file)
    except ValueError as exc:
        print(f"Error: invalid option. {exc}", file=sys.stderr)
        return 1
    except DownloadError as exc:
        message = str(exc)
        print(f"Error: download failed. {message}", file=sys.stderr)
        if "confirm you" in message.lower() and "not a bot" in message.lower():
            print_auth_hint(args)
        return 1
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print("Done.")
    print(f"Audio file: {audio_file}")
    print(f"Vocals file: {vocals_file}")
    print(f"Music file: {music_file}")
    print(f"Muted video file: {muted_video_file}")
    print(f"Subtitle file: {subtitle_file}")
    print(f"Karaoke file: {karaoke_file}")
    print(f"Karaoke subtitle file: {karaoke_subtitle_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
