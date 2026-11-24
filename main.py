"""WaveCrate - Automated Playlist Audio Downloader & MP3 Converter.

This script reads a playlist CSV containing track metadata and YouTube links,
resolves sources, downloads authorized audio streams, and converts them to
high-bitrate MP3 files using yt-dlp and FFmpeg.

Author: WaveCrate Contributors
License: MIT
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError, ExtractorError
except ImportError:
    YoutubeDL = None
    DownloadError = Exception
    ExtractorError = Exception


# ==============================================================================
# Dependency & Environment Checks
# ==============================================================================

def check_dependencies(ffmpeg_location: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """Verify that external dependencies (yt-dlp and FFmpeg) are available.

    Args:
        ffmpeg_location: Optional explicit path to the FFmpeg directory or binary.

    Returns:
        Tuple of (is_satisfied, error_message).
    """
    if YoutubeDL is None:
        return (
            False,
            "The 'yt-dlp' library is not installed.\n"
            "Please install dependencies with: pip install -r requirements.txt"
        )

    # Check for FFmpeg
    ffmpeg_found = False
    if ffmpeg_location:
        loc_path = Path(ffmpeg_location)
        if loc_path.is_file() and os.access(loc_path, os.X_OK):
            ffmpeg_found = True
        elif loc_path.is_dir():
            ffmpeg_found = shutil.which("ffmpeg", path=str(loc_path)) is not None
    else:
        ffmpeg_found = shutil.which("ffmpeg") is not None

    if not ffmpeg_found:
        return (
            False,
            "FFmpeg was not found on your system PATH.\n"
            "FFmpeg is required to convert downloaded audio streams into MP3 files.\n"
            "Installation instructions:\n"
            "  - Windows: winget install Gyan.FFmpeg (or download from https://ffmpeg.org/)\n"
            "  - macOS: brew install ffmpeg\n"
            "  - Linux: sudo apt install ffmpeg\n"
            "Alternatively, pass the directory path using: --ffmpeg-location <path>"
        )

    return True, None


# ==============================================================================
# Filename Sanitization & String Helpers
# ==============================================================================

def sanitize_filename(name: str, max_length: int = 150) -> str:
    """Sanitize a string to create a safe, portable filename across OS platforms.

    Strips reserved characters (<>:"/\\|?* and ASCII control codes), removes
    leading/trailing whitespace or dots, checks against reserved Windows device
    names, and limits overall length.

    Args:
        name: The raw string to sanitize.
        max_length: Maximum allowed character length for the filename stem.

    Returns:
        A sanitized, safe filename string.
    """
    if not name:
        return "unnamed_track"

    # Remove illegal characters across Windows, Linux, and macOS
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name))

    # Remove non-printable and invisible characters
    cleaned = re.sub(r"[\u200b-\u200d\uFEFF]", "", cleaned)

    # Strip surrounding dots and spaces (problematic on Windows filesystems)
    cleaned = cleaned.strip(". ")

    # Check against Windows reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9)
    reserved_names = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10))
    }
    if cleaned.upper() in reserved_names:
        cleaned = f"_{cleaned}"

    # Truncate if exceeds maximum length
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(". ")

    return cleaned or "unnamed_track"


def build_track_filename(
    artist: str,
    title: str,
    track_number: Optional[int] = None,
    include_number: bool = False
) -> str:
    """Generate a meaningful, standardized filename for a music track.

    Args:
        artist: Artist name.
        title: Song title.
        track_number: Optional track index/number.
        include_number: Whether to prefix the filename with track_number.

    Returns:
        Clean filename string without extension.
    """
    clean_artist = sanitize_filename(artist)
    clean_title = sanitize_filename(title)

    # If artist is identical to unnamed_track, fallback to title only
    if clean_artist == "unnamed_track" and clean_title != "unnamed_track":
        base_name = clean_title
    elif clean_title == "unnamed_track" and clean_artist != "unnamed_track":
        base_name = clean_artist
    elif clean_artist and clean_title:
        base_name = f"{clean_artist} - {clean_title}"
    else:
        base_name = "unnamed_track"

    if include_number and track_number is not None:
        return f"{track_number:02d} - {base_name}"

    return base_name


def is_valid_url(url: str) -> bool:
    """Validate whether a string is a reachable HTTP/HTTPS URL.

    Args:
        url: URL string to test.

    Returns:
        True if the URL format appears valid, False otherwise.
    """
    if not url or not isinstance(url, str):
        return False

    url_clean = url.strip()
    if url_clean.lower() in {"not found", "none", "n/a", "null", ""}:
        return False

    return url_clean.startswith("http://") or url_clean.startswith("https://")


# ==============================================================================
# CSV Ingestion
# ==============================================================================

def read_playlist_csv(csv_path: Path) -> List[Dict[str, Any]]:
    """Parse a playlist CSV file and extract track metadata.

    Expected columns: number, artist, title, youtube_link.
    Supports flexible case-insensitive header matching.

    Args:
        csv_path: Path to the input CSV file.

    Returns:
        List of dictionaries with normalized keys: 'number', 'artist',
        'title', 'youtube_link', and 'row_index'.

    Raises:
        FileNotFoundError: If the CSV file does not exist.
        ValueError: If the CSV file is empty or cannot be parsed.
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"Playlist CSV file not found: {csv_path.resolve()}")

    tracks: List[Dict[str, Any]] = []

    # Open with utf-8-sig to automatically strip any UTF-8 BOM byte sequence
    with open(csv_path, mode="r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)

        if reader.fieldnames is None:
            raise ValueError(f"The CSV file '{csv_path.name}' appears to be empty.")

        # Map lowercased and stripped headers to actual header names
        header_map = {col.strip().lower(): col for col in reader.fieldnames if col}

        # Identify target column names
        num_col = (
            header_map.get("number")
            or header_map.get("#")
            or header_map.get("index")
            or header_map.get("track_number")
        )
        artist_col = (
            header_map.get("artist")
            or header_map.get("artists")
            or header_map.get("singer")
            or header_map.get("creator")
            or header_map.get("performer")
        )
        title_col = (
            header_map.get("title")
            or header_map.get("track")
            or header_map.get("song")
            or header_map.get("track_title")
        )
        link_col = (
            header_map.get("youtube_link")
            or header_map.get("youtube_url")
            or header_map.get("youtube")
            or header_map.get("link")
            or header_map.get("url")
        )

        for row_idx, row in enumerate(reader, start=2):
            # Extract fields with safe fallbacks
            raw_num = row.get(num_col, "").strip() if num_col else ""
            artist = row.get(artist_col, "").strip() if artist_col else ""
            title = row.get(title_col, "").strip() if title_col else ""
            youtube_link = row.get(link_col, "").strip() if link_col else ""

            # Skip entirely blank rows
            if not artist and not title and not youtube_link:
                continue

            # Parse number or fallback to sequential order
            try:
                track_number = int(raw_num) if raw_num else (len(tracks) + 1)
            except ValueError:
                track_number = len(tracks) + 1

            tracks.append({
                "number": track_number,
                "artist": artist,
                "title": title,
                "youtube_link": youtube_link,
                "row_index": row_idx,
            })

    if not tracks:
        raise ValueError(f"No valid song entries were found in '{csv_path.name}'.")

    return tracks


# ==============================================================================
# YouTube Source Matching & Audio Downloading
# ==============================================================================

def match_youtube_source(artist: str, title: str) -> Optional[str]:
    """Search YouTube for the top audio match when a link is missing.

    Args:
        artist: Artist name.
        title: Song title.

    Returns:
        Direct video URL if found, or None.
    """
    query = f"{artist} {title}".strip()
    if not query:
        return None

    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            search_results = ydl.extract_info(f"ytsearch1:{query}", download=False)
            entries = search_results.get("entries", [])
            if entries and entries[0]:
                first = entries[0]
                url = first.get("url")
                if not url and first.get("id"):
                    url = f"https://www.youtube.com/watch?v={first['id']}"
                return url
    except Exception:
        return None

    return None


def download_and_convert_track(
    song: Dict[str, Any],
    output_dir: Path,
    bitrate: int = 320,
    overwrite: bool = False,
    include_number: bool = False,
    auto_search: bool = True,
    ffmpeg_location: Optional[str] = None,
) -> Tuple[str, str, Optional[str]]:
    """Download audio stream for a track and convert it to MP3 format.

    Args:
        song: Track metadata dictionary.
        output_dir: Directory where the converted MP3 should be stored.
        bitrate: Audio bitrate in kbps (e.g., 320, 256, 192).
        overwrite: Overwrite existing destination files.
        include_number: Include track number in filename.
        auto_search: Auto-search YouTube if link is missing or invalid.
        ffmpeg_location: Optional path to FFmpeg directory/binary.

    Returns:
        Tuple of (status, message, output_filepath).
        Status is one of: "SUCCESS", "SKIPPED", "FAILED".
    """
    artist = song.get("artist", "")
    title = song.get("title", "")
    number = song.get("number")
    raw_link = song.get("youtube_link", "").strip()

    # Build standardized target filename
    base_stem = build_track_filename(artist, title, number, include_number)
    target_mp3 = output_dir / f"{base_stem}.mp3"

    # Duplicate detection & overwrite protection
    if target_mp3.exists() and not overwrite:
        return "SKIPPED", f"File already exists: {target_mp3.name}", str(target_mp3)

    # Resolve YouTube URL
    resolved_url = raw_link if is_valid_url(raw_link) else None

    if not resolved_url:
        if auto_search and (artist or title):
            resolved_url = match_youtube_source(artist, title)
            if not resolved_url:
                return (
                    "FAILED",
                    f"No YouTube match found for query: '{artist} - {title}'",
                    None
                )
        else:
            return (
                "FAILED",
                f"Missing or invalid YouTube link: '{raw_link or 'None'}'",
                None
            )

    # yt-dlp output template (escape '%' as '%%' to prevent template expansion errors)
    safe_stem_escaped = base_stem.replace("%", "%%")
    outtmpl = str(output_dir / f"{safe_stem_escaped}.%(ext)s")

    ydl_opts: Dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(bitrate),
        }],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
        "continuedl": True,
    }

    if ffmpeg_location:
        ydl_opts["ffmpeg_location"] = str(ffmpeg_location)

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([resolved_url])

        if target_mp3.exists():
            return "SUCCESS", f"Saved: {target_mp3.name} ({bitrate} kbps)", str(target_mp3)
        return "SUCCESS", f"Processed successfully: {base_stem}.mp3", str(target_mp3)

    except (DownloadError, ExtractorError) as err:
        # Extract clean error message without noisy stack traces
        err_msg = str(err).splitlines()[-1] if str(err) else "Download failed"
        return "FAILED", f"yt-dlp error: {err_msg}", None
    except Exception as err:
        return "FAILED", f"Unexpected error: {err}", None


# ==============================================================================
# Batch Processing & Progress Reporting
# ==============================================================================

def print_summary(
    total_tracks: int,
    stats: Dict[str, int],
    failed_items: List[Dict[str, str]],
    output_dir: Path
) -> None:
    """Display a clean, formatted batch download summary.

    Args:
        total_tracks: Total tracks in CSV.
        stats: Dictionary containing count of SUCCESS, SKIPPED, FAILED.
        failed_items: List of failed track descriptions and error messages.
        output_dir: Destination folder path.
    """
    border = "=" * 70
    print(f"\n{border}")
    print("                      WAVECRATE BATCH SUMMARY")
    print(border)
    print(f"  Total Tracks in CSV  : {total_tracks}")
    print(f"  Successfully Converted: {stats.get('SUCCESS', 0)}")
    print(f"  Skipped (Existing)   : {stats.get('SKIPPED', 0)}")
    print(f"  Failed               : {stats.get('FAILED', 0)}")
    print(f"  Output Directory     : {output_dir.resolve()}")
    print(border)

    if failed_items:
        print("\nFailed Tracks:")
        for item in failed_items:
            print(f"  - [{item['track']}]: {item['reason']}")
        print()


def process_playlist(
    csv_path: Path,
    output_dir: Path,
    bitrate: int = 320,
    overwrite: bool = False,
    include_number: bool = False,
    auto_search: bool = True,
    ffmpeg_location: Optional[str] = None,
) -> None:
    """Process all tracks from a CSV file sequentially with progress reporting.

    Args:
        csv_path: Path to the input CSV file.
        output_dir: Target folder for converted MP3 files.
        bitrate: Audio quality bitrate in kbps.
        overwrite: Overwrite existing output files.
        include_number: Prefix filenames with track numbers.
        auto_search: Auto-search YouTube if link is missing.
        ffmpeg_location: Optional path to FFmpeg binary/folder.
    """
    # Ensure output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)

    print("\nReading playlist CSV...")
    tracks = read_playlist_csv(csv_path)
    total = len(tracks)
    print(f"Loaded {total} track(s) from '{csv_path.name}'.")
    print(f"Target Bitrate   : {bitrate} kbps")
    print(f"Output Directory : {output_dir.resolve()}\n")

    stats = {"SUCCESS": 0, "SKIPPED": 0, "FAILED": 0}
    failed_items: List[Dict[str, str]] = []

    try:
        for idx, song in enumerate(tracks, start=1):
            track_label = f"{song.get('artist', 'Unknown')} - {song.get('title', 'Unknown')}"
            link_display = song.get("youtube_link") or "[Auto-Search Query]"

            print(f"[{idx}/{total}] Processing: {track_label}")
            print(f"       Source : {link_display}")

            status, message, _ = download_and_convert_track(
                song=song,
                output_dir=output_dir,
                bitrate=bitrate,
                overwrite=overwrite,
                include_number=include_number,
                auto_search=auto_search,
                ffmpeg_location=ffmpeg_location,
            )

            stats[status] = stats.get(status, 0) + 1

            if status == "SUCCESS":
                print(f"       Status : [SUCCESS] {message}\n")
            elif status == "SKIPPED":
                print(f"       Status : [SKIPPED] {message}\n")
            else:
                print(f"       Status : [FAILED] {message}\n")
                failed_items.append({"track": track_label, "reason": message})

    except KeyboardInterrupt:
        print("\n\n[!] Operation cancelled by user (Ctrl+C). Generating summary for processed tracks...")
        print_summary(total, stats, failed_items, output_dir)
        sys.exit(130)

    # Print final summary upon normal completion
    print_summary(total, stats, failed_items, output_dir)


# ==============================================================================
# CLI Argument Parser & Entry Point
# ==============================================================================

def parse_arguments() -> argparse.Namespace:
    """Configure command-line arguments for WaveCrate.

    Returns:
        Parsed arguments namespace.
    """
    parser = argparse.ArgumentParser(
        prog="WaveCrate",
        description="Automated playlist processor, YouTube matcher, and MP3 converter.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "csv_file",
        nargs="?",
        default="playlist.csv",
        help="Path to the playlist CSV file (columns: number, artist, title, youtube_link)",
    )
    parser.add_argument(
        "-o", "--output",
        default="downloads",
        help="Destination directory for converted MP3 audio files",
    )
    parser.add_argument(
        "-b", "--bitrate",
        type=int,
        default=320,
        choices=[128, 192, 256, 320],
        help="Target MP3 audio bitrate in kbps",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing MP3 files instead of skipping them",
    )
    parser.add_argument(
        "--track-numbers",
        action="store_true",
        help="Prefix output filenames with sequential track numbers (e.g., '01 - Artist - Title.mp3')",
    )
    parser.add_argument(
        "--no-search",
        action="store_true",
        help="Disable automatic YouTube search when youtube_link is empty or missing",
    )
    parser.add_argument(
        "--ffmpeg-location",
        default=None,
        help="Custom path to the FFmpeg executable or binary directory",
    )

    return parser.parse_args()


def main() -> None:
    """Main program entry point."""
    args = parse_arguments()

    # Verify external dependencies before proceeding
    ok, err_msg = check_dependencies(args.ffmpeg_location)
    if not ok:
        print(f"\n[ERROR] Missing Dependency:\n{err_msg}", file=sys.stderr)
        sys.exit(1)

    csv_path = Path(args.csv_file)
    output_dir = Path(args.output)

    try:
        process_playlist(
            csv_path=csv_path,
            output_dir=output_dir,
            bitrate=args.bitrate,
            overwrite=args.overwrite,
            include_number=args.track_numbers,
            auto_search=not args.no_search,
            ffmpeg_location=args.ffmpeg_location,
        )
    except FileNotFoundError as fnf_err:
        print(f"\n[ERROR] {fnf_err}", file=sys.stderr)
        sys.exit(1)
    except ValueError as val_err:
        print(f"\n[ERROR] Invalid CSV Data: {val_err}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"\n[ERROR] An unexpected error occurred: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
