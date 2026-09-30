#!/usr/bin/env python3
"""
video_to_gif.py — Turn video (files, URLs, or stdin) into optimized animated
GIF / WebP / APNG / MP4 with FFmpeg.

Highlights
  * Two-pass palette GIFs (palettegen -> paletteuse) with
    --colors / --dither control, optional gifsicle post-optimization (-O/--lossy)
    or the gifski engine (--engine gifski)
  * Crop, --auto-crop (cropdetect), speed, reverse, seamless --boomerang,
    seamless crossfade loops (--fade), caption overlay (--text)
  * --max-size: proportional/bisection search over width (then fps) that lands on
    the widest result that fits
  * Batch mode (many inputs, -j parallel jobs), '-' for stdin, URLs via yt-dlp
  * ~/.config/video_to_gif/config.toml defaults, --open, --copy, --verbose, --quiet
  * MP4 audio (--audio), hardware decode (--hwaccel), thread caps (--threads)

Logs go to stderr; stdout only ever carries the resulting file path(s) (or the
commands in --dry-run), so the tool composes cleanly in pipes.

Examples:
  video_to_gif.py clip.mp4
  video_to_gif.py -s 5 -d 3 -w 640 -q high clip.mp4 -o demo.gif
  video_to_gif.py -c 640x360+0+60 -S 2 clip.mp4
  video_to_gif.py --max-size 5M --boomerang -O clip.mp4
  video_to_gif.py --webp -q high --auto-crop clip.mp4
  video_to_gif.py --fade 0.5 --text "when the build passes" clip.mp4
  video_to_gif.py -j 4 --out-dir gifs/ *.mp4
  video_to_gif.py https://example.com/watch?v=abc -s 12 -d 4
  cat clip.mp4 | video_to_gif.py - -o out.gif
  video_to_gif.py --preview -s 12 clip.mp4

Config file (TOML, top-level keys = long option names):
  # ~/.config/video_to_gif/config.toml
  fps = 12
  width = 640
  quality = "high"
  optimize = 3

Requires: ffmpeg + ffprobe (sudo dnf install ffmpeg)
Optional: gifsicle (-O/--lossy), gifski (--engine gifski), yt-dlp (URL input),
          wl-copy / xclip / xsel / pbcopy (--copy), xdg-open / open (--open)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import functools
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

# ── Tokyo Night palette (stderr only; off when piped or NO_COLOR is set) ───────
_COLOR = sys.stderr.isatty() and not os.environ.get("NO_COLOR")


def _rgb(r: int, g: int, b: int) -> str:
    return f"\033[38;2;{r};{g};{b}m" if _COLOR else ""


BLUE = _rgb(0x7A, 0xA2, 0xF7)
GREEN = _rgb(0x9E, 0xCE, 0x6A)
YELLOW = _rgb(0xE0, 0xAF, 0x68)
RED = _rgb(0xF7, 0x76, 0x8E)
CYAN = _rgb(0x7D, 0xCF, 0xFF)
PURPLE = _rgb(0xBB, 0x9A, 0xF7)
DIM = _rgb(0x56, 0x5F, 0x89)
BOLD = "\033[1m" if _COLOR else ""
RESET = "\033[0m" if _COLOR else ""

# ── Constants ─────────────────────────────────────────────────────────────────
VERSION = "1.1.0"
QUALITY = {
    "low":    dict(colors=64,  dither="bayer",           bayer_scale=5, webp=50, crf=30, gifski=50),
    "medium": dict(colors=128, dither="bayer",           bayer_scale=3, webp=70, crf=26, gifski=80),
    "high":   dict(colors=256, dither="floyd_steinberg", bayer_scale=2, webp=90, crf=20, gifski=100),
}
DITHERS = ("bayer", "heckbert", "floyd_steinberg", "sierra2", "sierra2_4a",
           "sierra3", "burkes", "atkinson", "none")
FORMATS = ("gif", "webp", "apng", "mp4")
EXT = {"gif": ".gif", "webp": ".webp", "apng": ".apng", "mp4": ".mp4"}
SUFFIX_FORMAT = {".gif": "gif", ".webp": "webp", ".apng": "apng", ".png": "apng", ".mp4": "mp4"}

MIN_FPS = 5.0
MIN_WIDTH = 120
MAX_ATTEMPTS = 8
RAM_WARN_BYTES = 1 << 30

TIME_RE = re.compile(r"^(?:(?:\d+:)?\d{1,2}:)?\d+(?:\.\d+)?$")
CROP_RE = re.compile(r"^(\d+)x(\d+)\+(\d+)\+(\d+)$")
SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMG])?B?$", re.IGNORECASE)  # [fix] plain bytes allowed
URL_RE = re.compile(r"^https?://", re.IGNORECASE)
KV_RE = re.compile(r"^([A-Za-z0-9_]+)=(.*)$")
PROGRESS_KEYS = {
    "frame", "fps", "bitrate", "total_size", "out_time", "out_time_us",
    "out_time_ms", "dup_frames", "drop_frames", "speed", "progress",
}
CONFIG_DENY = {"input", "output", "config", "no_config", "preview", "dry_run", "help"}

# Runtime switches, set in main()
VERBOSE = False
QUIET = False          # [new] -Q/--quiet
SHOW_PROGRESS = False
INTERACTIVE = False


class ConvError(Exception):
    """A user-facing failure (bad input, FFmpeg error, ...)."""


class Skipped(Exception):
    """The user declined to overwrite an existing file."""


# ── Logging (thread-safe, with an optional per-job prefix for batch mode) ─────
_LOCK = threading.Lock()
_tls = threading.local()


def _emit(text: str = "", *, stream=None) -> None:
    with _LOCK:
        print(text, file=stream or sys.stderr, flush=True)


def _log(tag: str, color: str, msg: str) -> None:
    # [fix] --quiet suppresses INFO/OK chatter; warnings and errors always show
    if QUIET and (tag.startswith("[INFO]") or tag.startswith("[OK]")):
        return
    prefix = getattr(_tls, "tag", "")
    _emit(f"{color}{tag}{RESET} {prefix}{msg}")


def info(msg: str) -> None:
    _log("[INFO] ", CYAN, msg)


def success(msg: str) -> None:
    _log("[OK]   ", GREEN, msg)


def warn(msg: str) -> None:
    _log("[WARN] ", YELLOW, msg)


def fail(msg: str) -> None:
    _log("[ERROR]", RED, msg)


def die(msg: str) -> NoReturn:
    raise ConvError(msg)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} GB"


# ── Argument types ────────────────────────────────────────────────────────────
def t_time(s: str) -> float:
    """'5', '5.5', '01:05', '00:01:05.5' -> seconds."""
    if isinstance(s, (int, float)):
        return float(s)
    if not TIME_RE.match(str(s)):
        raise argparse.ArgumentTypeError(f"invalid time {s!r} (use seconds or [HH:]MM:SS)")
    secs = 0.0
    for part in str(s).split(":"):
        secs = secs * 60 + float(part)
    return secs


def t_positive_time(s: str) -> float:
    """Like t_time, but 0 makes no sense for --duration. [fix] -d 0 used to mean 'to end'."""
    v = t_time(s)
    if v <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0 (got {s})")
    return v


def t_positive_float(s: str) -> float:
    try:
        v = float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {s!r}") from None
    if not math.isfinite(v) or v <= 0:  # [fix] reject inf/nan (e.g. --speed inf)
        raise argparse.ArgumentTypeError(f"must be a finite number > 0 (got {s})")
    return v


def t_crop(s: str) -> tuple[int, int, int, int]:
    m = CROP_RE.match(str(s))
    if not m:
        raise argparse.ArgumentTypeError(f"format must be WxH+X+Y (got {s!r})")
    w, h, x, y = map(int, m.groups())
    if w == 0 or h == 0:
        raise argparse.ArgumentTypeError("crop width/height must be > 0")
    return w, h, x, y


def t_size(s: str) -> int:
    if isinstance(s, (int, float)):
        return int(s)
    m = SIZE_RE.match(s.strip())
    if not m:
        raise argparse.ArgumentTypeError("format must be a number + optional K, M or G (e.g. 5M, 500K, 300000)")
    mult = {"K": 1024, "M": 1024**2, "G": 1024**3}.get((m.group(2) or "").upper(), 1)
    n = int(float(m.group(1)) * mult)
    if n <= 0:
        raise argparse.ArgumentTypeError("size must be > 0")
    return n


def t_int_range(lo: int, hi: int):
    def conv(s: str) -> int:
        try:
            v = int(s)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not an integer: {s!r}") from None
        if not lo <= v <= hi:
            raise argparse.ArgumentTypeError(f"must be between {lo} and {hi} (got {v})")
        return v
    return conv


def config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "video_to_gif" / "config.toml"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="video_to_gif.py",
        description="Convert video (files, URLs, stdin) into optimized animated GIF/WebP/APNG/MP4.",
        epilog="Examples:" + __doc__.split("Examples:")[1].split("Requires:")[0].rstrip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("input", nargs="+",
                   help="input video file(s); '-' reads stdin; http(s) URLs use yt-dlp")
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")  # [new]

    g = p.add_argument_group("output")
    g.add_argument("-o", "--output", help="output file (single input only)")
    g.add_argument("--out-dir", metavar="DIR", help="write outputs into DIR (batch-friendly)")
    g.add_argument("-F", "--format", choices=FORMATS,
                   help="output format (default: from --output suffix, else gif)")
    g.add_argument("--webp", dest="format", action="store_const", const="webp", help="shortcut for -F webp")
    g.add_argument("--apng", dest="format", action="store_const", const="apng", help="shortcut for -F apng")
    g.add_argument("--mp4", dest="format", action="store_const", const="mp4", help="shortcut for -F mp4")
    g.add_argument("--audio", action="store_true",  # [new]
                   help="MP4 only: mux in the source audio as AAC 128k "
                        "(not affected by speed/reverse/boomerang/fade)")
    g.add_argument("--engine", choices=("ffmpeg", "gifski"), default="ffmpeg",
                   help="GIF encoder backend (default: ffmpeg)")

    g = p.add_argument_group("clip")
    g.add_argument("-s", "--start", type=t_time, default=0.0, metavar="TIME",
                   help="start time, seconds or [HH:]MM:SS (default: 0)")
    g.add_argument("-d", "--duration", type=t_positive_time, metavar="TIME",  # [fix] was t_time
                   help="duration (default: to end)")
    g.add_argument("-e", "--end", type=t_time, metavar="TIME", help="end time (alternative to --duration)")
    g.add_argument("-r", "--fps", type=t_positive_float, default=15.0, help="frames per second (default: 15)")
    g.add_argument("-w", "--width", type=int, default=480, metavar="PX",
                   help="output width; never upscales; 0 or -1 keeps source width (default: 480)")
    g.add_argument("-l", "--loop", type=int, default=0, metavar="N",
                   help="0 = loop forever, -1 = play once, N = repeat N extra times "
                        "(GIF/WebP/APNG; ignored for mp4)")
    g.add_argument("-c", "--crop", type=t_crop, metavar="WxH+X+Y",
                   help="crop region applied before scaling, e.g. 640x360+0+60")
    g.add_argument("--auto-crop", action="store_true", help="detect and trim black bars (cropdetect)")

    g = p.add_argument_group("motion & effects")
    g.add_argument("-S", "--speed", type=t_positive_float, default=1.0, metavar="FACTOR",
                   help="playback speed multiplier, e.g. 2.0 = 2x faster")
    motion = g.add_mutually_exclusive_group()
    motion.add_argument("-R", "--reverse", action="store_true", help="play the clip backwards")
    motion.add_argument("-b", "--boomerang", action="store_true",
                        help="forward then backward, with the pivot frames de-duplicated")
    g.add_argument("--fade", type=t_positive_float, metavar="SECS",
                   help="seamless loop: crossfade the last SECS of the clip into its start")
    g.add_argument("--text", help="caption overlay (use \\n for line breaks)")
    g.add_argument("--text-pos", choices=("top", "center", "bottom"), default="bottom")
    g.add_argument("--text-size", type=int, metavar="PX", help="caption font size (default: ~height/12)")
    g.add_argument("--font", metavar="FILE", help="font file for --text (default: fontconfig)")

    g = p.add_argument_group("quality")
    g.add_argument("-q", "--quality", choices=QUALITY, default="medium",
                   help="preset for palette size, dithering, WebP/x264/gifski quality (default: medium)")
    g.add_argument("--colors", type=t_int_range(2, 256), metavar="N", help="override palette size (GIF)")
    g.add_argument("--dither", choices=DITHERS, help="override paletteuse dither algorithm")
    g.add_argument("--bayer-scale", type=t_int_range(0, 5), metavar="0-5", help="bayer dither scale")
    g.add_argument("--stats-mode", choices=("diff", "full"), default="diff", help="palettegen stats mode")
    g.add_argument("--diff-mode", choices=("rectangle", "none"), default="none",
                   help="paletteuse diff mode. FFmpeg's GIF encoder already diffs frames by default "
                        "(no size change on 6.x in testing); 'rectangle' may help older builds")
    g.add_argument("--x264-preset",  # [new]
                   choices=("ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
                            "slow", "slower", "veryslow", "placebo"),
                   default="medium", help="x264 speed/size preset for MP4 output (default: medium)")
    g.add_argument("-O", "--optimize", nargs="?", const=3, type=t_int_range(1, 3), metavar="LEVEL",
                   help="post-process GIFs with gifsicle -O<LEVEL> (default level 3)")
    g.add_argument("--lossy", type=t_int_range(1, 200), metavar="N",
                   help="gifsicle lossy compression (implies -O; try 30-80)")
    g.add_argument("-m", "--max-size", type=t_size, metavar="SIZE",
                   help="target max size (e.g. 5M, 500K, 300000); searches width, then fps, until it fits")

    g = p.add_argument_group("run")
    g.add_argument("-p", "--preview", action="store_true", help="extract one PNG frame at --start and exit")
    g.add_argument("-j", "--jobs", type=t_int_range(1, 64), default=1, metavar="N",
                   help="convert N inputs in parallel (default: 1)")
    g.add_argument("--hwaccel", metavar="NAME",  # [new]
                   help="hardware-accelerated decoding, passed to ffmpeg as -hwaccel NAME "
                        "(e.g. auto, vaapi, cuda)")
    g.add_argument("--threads", type=t_int_range(1, 256), metavar="N",  # [new]
                   help="cap FFmpeg encode/filter threads (avoids oversubscription with -j)")
    g.add_argument("--open", action="store_true", help="open the result when done")
    g.add_argument("--copy", action="store_true", help="copy the result path to the clipboard")
    g.add_argument("-v", "--verbose", action="store_true", help="show FFmpeg commands and warnings")
    g.add_argument("-Q", "--quiet", action="store_true",  # [new]
                   help="suppress INFO/OK logs and the progress bar (stdout still carries the paths)")
    g.add_argument("--force", action="store_true", help="overwrite outputs without prompting")
    g.add_argument("--dry-run", action="store_true", help="print commands, run nothing")
    g.add_argument("--config", metavar="FILE", help=f"config file (default: {config_path()})")
    g.add_argument("--no-config", action="store_true", help="ignore the config file")
    return p


def load_config(argv: list[str], parser: argparse.ArgumentParser) -> None:
    """Apply config-file values as parser defaults (the command line still wins)."""
    pre = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    pre.add_argument("--config")
    pre.add_argument("--no-config", action="store_true")
    known, _ = pre.parse_known_args(argv)
    if known.no_config:
        return
    path = Path(known.config) if known.config else config_path()
    if not path.is_file():
        if known.config:
            die(f"Config file not found: {path}")
        return
    if tomllib is None:
        warn(f"Python 3.11+ is required to read {path}; ignoring config.")
        return
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        die(f"Could not read config {path}: {e}")

    actions: dict[str, argparse.Action] = {}
    for a in parser._actions:  # --webp/--apng/--mp4 share dest 'format' with -F; keep the one with choices
        if a.dest not in actions or (a.choices and not actions[a.dest].choices):
            actions[a.dest] = a
    cfg: dict[str, object] = {}
    for key, val in raw.items():
        dest = key.replace("-", "_")
        act = actions.get(dest)
        if act is None or dest in CONFIG_DENY:
            warn(f"{path.name}: ignoring unknown/unsupported key {key!r}")
            continue
        if act.choices and val not in act.choices:
            die(f"{path.name}: {key} = {val!r} is not one of {list(act.choices)}")
        # [fix] route *every* TOML scalar through the option's own type/range checks.
        # Previously only strings were coerced, so e.g. `optimize = true` reached
        # gifsicle as "-OTrue" and `colors = 999` skipped its 2-256 range check.
        if isinstance(val, bool):
            if act.type is not None:
                die(f"{path.name}: {key} = true/false is invalid for this option")
        elif isinstance(val, (int, float)):
            if act.type is None:  # flag option (store_true) — needs a real bool
                die(f"{path.name}: {key} must be true/false (it is a flag option)")
            try:
                val = act.type(str(val))  # runs range checks too
            except (argparse.ArgumentTypeError, ValueError) as e:
                die(f"{path.name}: {key}: {e}")
        elif isinstance(val, str) and act.type is not None:
            try:
                val = act.type(val)
            except (argparse.ArgumentTypeError, ValueError) as e:
                die(f"{path.name}: {key}: {e}")
        cfg[dest] = val
    parser.set_defaults(**cfg)


def _protect_optimize(argv: list[str]) -> list[str]:
    """-O takes an optional level (nargs='?'), which would otherwise swallow the next
    positional ('-O clip.mp4'). Insert the default level unless a real 1-3 follows."""
    out: list[str] = []
    for i, tok in enumerate(argv):
        if tok == "--":
            out.extend(argv[i:])
            break
        out.append(tok)
        if tok in ("-O", "--optimize"):
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if nxt not in ("1", "2", "3"):
                out.append("3")
    return out


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    argv = _protect_optimize(list(sys.argv[1:] if argv is None else argv))
    parser = build_parser()
    load_config(argv, parser)
    args = parser.parse_args(argv)

    if args.end is not None:
        if args.duration is not None:
            parser.error("use either --duration or --end, not both")
        if args.end <= args.start:
            parser.error("--end must be after --start")
        args.duration = args.end - args.start
    if args.reverse and args.boomerang:  # config-file defaults bypass argparse's group check
        parser.error("--reverse and --boomerang are mutually exclusive")
    if args.crop and args.auto_crop:
        parser.error("--crop and --auto-crop are mutually exclusive")
    if args.fade and args.boomerang:
        parser.error("--fade and --boomerang are mutually exclusive (a boomerang already loops)")
    if args.fade and args.reverse:  # [fix] the crossfade would land on the wrong end of the clip
        parser.error("--fade and --reverse cannot be combined")
    if args.output and len(args.input) > 1:
        parser.error("--output only works with a single input; use --out-dir for batches")
    if args.input.count("-") > 1:
        parser.error("stdin ('-') can only be given once")
    if args.lossy and not args.optimize:
        args.optimize = 3
    seen: dict[tuple[str, str], str] = {}
    seen_specs: set[str] = set()
    for spec in args.input:  # two inputs mapping to the same output would silently clobber each other
        if spec == "-" or URL_RE.match(spec):
            # [fix] duplicate identical URL/stdin specs (file-path collisions checked below)
            if spec in seen_specs:
                parser.error(f"{spec!r} is given more than once")
            seen_specs.add(spec)
            continue
        p = Path(spec)
        key = (str(Path(args.out_dir) if args.out_dir else p.parent), p.stem)
        if key in seen:
            parser.error(f"{spec!r} and {seen[key]!r} would write the same output file; "
                         "use separate --out-dir runs or rename one")
        seen[key] = spec
    # [fix] resolve the effective format *before* validating --engine, so
    # '-o out.webp --engine gifski' is rejected instead of silently ignored
    if args.engine == "gifski" and pick_format(args) != "gif":
        parser.error("--engine gifski only produces GIFs")
    return args


# ── Probing & capabilities ────────────────────────────────────────────────────
@dataclass
class Source:
    width: int
    height: int
    duration: float | None


def _float_or_none(v: object) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def probe(path: Path) -> Source:
    r = _tracked_run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,duration:stream_tags=rotate:stream_side_data=rotation:format=duration",
         "-of", "json", str(path)],
        capture_output=True)
    if r.returncode != 0:
        die(f"ffprobe could not read '{path}':\n{(r.stderr or '').strip()}")
    try:  # [fix] malformed probe output -> clean error, not a traceback
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError as e:
        die(f"ffprobe returned unparseable data for '{path}': {e}")
    streams = data.get("streams") or []
    if not streams:
        die(f"Input file does not contain a valid video stream: {path}")
    s = streams[0]
    try:  # [fix] streams without width/height used to raise a bare KeyError
        w, h = int(s["width"]), int(s["height"])
    except (KeyError, TypeError, ValueError):
        die(f"Could not determine the dimensions of '{path}' — not a decodable video?")
    # ffmpeg auto-rotates on decode, so frames seen by the filters are swapped for 90/270 degree tags
    rot = _float_or_none((s.get("tags") or {}).get("rotate"))
    if rot is None:
        for sd in s.get("side_data_list") or []:
            if _float_or_none(sd.get("rotation")) is not None:
                rot = _float_or_none(sd.get("rotation"))
                break
    if rot is not None and round(abs(rot)) % 180 == 90:
        w, h = h, w
    dur = _float_or_none(s.get("duration")) or _float_or_none(data.get("format", {}).get("duration"))
    return Source(w, h, dur)


@functools.lru_cache(maxsize=None)
def ff_names(kind: str) -> frozenset[str]:
    out = _tracked_run(["ffmpeg", "-hide_banner", f"-{kind}"], capture_output=True).stdout or ""
    return frozenset(re.findall(r"^\s*[A-Z.]{2,6}\s+(\S+)", out, re.MULTILINE))


def require_ffmpeg(kind: str, name: str, why: str) -> None:
    if name not in ff_names(kind):
        die(f"Your FFmpeg build has no '{name}' {kind[:-1]} (needed for {why}).")


def plays(loop: int) -> int:
    """Map our loop semantics ('N extra repeats') to muxers that want a play count.

    The GIF muxer takes the NETSCAPE loop count verbatim (0 = forever, -1 = omit
    the extension = play once), so GIF passes --loop through unchanged; APNG's
    `plays` (and our mapping for WebP's `loop`) count *total* plays, hence
    0 -> 0, -1 -> 1, N -> N+1 here.
    """
    return 1 if loop < 0 else (0 if loop == 0 else loop + 1)


# ── Job description & filter graph ────────────────────────────────────────────
@dataclass
class Job:
    args: argparse.Namespace
    inp: Path
    src: Source
    fmt: str
    workdir: Path
    crop: tuple[int, int, int, int] | None
    width: int                    # requested effective width
    base_len: float | None        # clip length on the output timeline (after speed)
    out_len: float | None         # expected output length (progress bar)
    textfile: Path | None = None
    optimize: int | None = None
    lossy: int | None = None

    def out_height(self, width: int) -> int:
        cw, ch = (self.crop[0], self.crop[1]) if self.crop else (self.src.width, self.src.height)
        return max(2, round(width * ch / cw))


def time_flags(args: argparse.Namespace) -> list[str]:
    flags: list[str] = []
    if args.start:
        flags += ["-ss", f"{args.start:.3f}"]
    if args.duration:
        flags += ["-t", f"{args.duration:.3f}"]
    return flags


def fq(value: object) -> str:
    """Single-quote a value for an FFmpeg filter option."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def drawtext_filter(job: Job, width: int) -> str:
    a = job.args
    assert job.textfile is not None
    size = a.text_size or max(12, round(job.out_height(width) / 12))
    y = {"top": "10", "center": "(h-text_h)/2", "bottom": "h-text_h-10"}[a.text_pos]
    parts = [f"textfile={fq(job.textfile)}", "expansion=none", f"fontsize={size}",
             "fontcolor=white", "borderw=2", "bordercolor=black", "x=(w-text_w)/2", f"y={y}"]
    if a.font:
        parts.append(f"fontfile={fq(a.font)}")
    return "drawtext=" + ":".join(parts)


def build_graph(job: Job, width: int, fps: float) -> str:
    """Filter graph ending in the [g] pad.

    crop -> speed -> fps -> scale -> [fade loop] -> [reverse | boomerang] -> [text]
    fps is applied inside the graph so palettegen sees exactly the frames that
    end up in the output.
    """
    a = job.args
    pre: list[str] = []
    if job.crop:
        w, h, x, y = job.crop
        pre.append(f"crop={w}:{h}:{x}:{y}")
    if a.speed != 1.0:
        pre.append(f"setpts={1 / a.speed:.6f}*PTS")
    pre.append(f"fps={fps:g}")
    w_out = width - width % 2 if job.fmt == "mp4" else width  # x264 needs even dims
    pre.append(f"scale=w={w_out}:h=-2:flags=lanczos")

    graph = f"[0:v]{','.join(pre)}[c0]"
    cur = "c0"

    if a.fade:
        d = a.fade
        assert job.base_len is not None
        offset = max(0.0, job.base_len - 2 * d - 1 / fps)
        graph += (f";[{cur}]split[fa][fb]"
                  f";[fa]trim=start={d:.3f},setpts=PTS-STARTPTS[fmain]"
                  f";[fb]trim=end={d:.3f},setpts=PTS-STARTPTS[fhead]"
                  f";[fmain][fhead]xfade=transition=fade:duration={d:.3f}:offset={offset:.3f}[c1]")
        cur = "c1"
    if a.reverse:
        # [fix] the reverse filter replays buffered frames with their original
        # (decreasing) PTS; rebuild monotonically increasing timestamps from the
        # frame index (setpts idiom from the official docs).
        graph += f";[{cur}]reverse,setpts=N/{fps:g}/TB[c2]"
        cur = "c2"
    if a.boomerang:
        # forward 0..N-1, then N-2..1: both pivot frames are dropped so the loop doesn't stutter
        graph += (f";[{cur}]split[ba][bb]"
                  f";[bb]trim=start_frame=1,reverse,trim=start_frame=1,"
                  f"setpts=N/{fps:g}/TB[br]"  # [fix] same PTS rebuild for the reversed half
                  f";[ba][br]concat=n=2:v=1:a=0[c3]")
        cur = "c3"
    if a.text:
        graph += f";[{cur}]{drawtext_filter(job, width)}[c4]"
        cur = "c4"
    return graph + f";[{cur}]null[g]"


# ── Process runners ───────────────────────────────────────────────────────────
_ACTIVE: set[subprocess.Popen] = set()       # [fix] track in-flight children ...
_ACTIVE_LOCK = threading.RLock()              #       ... so SIGTERM/Ctrl-C can reap them


def _kill_active() -> None:
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE)
    for p in procs:
        try:
            p.kill()
        except OSError:
            pass


def _on_sigterm(signum: int, frame: object) -> None:
    # [fix] SIGTERM (kill/timeout/service managers) used to leave ffmpeg orphaned
    # and block interpreter shutdown on non-daemon pool threads.
    _kill_active()
    raise SystemExit(143)


def _tracked_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run that registers the child so _kill_active() can reap it.

    Text mode defaults to UTF-8 with replacement, so a stray non-UTF-8 byte in
    tool output can never raise UnicodeDecodeError mid-encode. [fix]
    """
    if kwargs.pop("capture_output", False):
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    proc = subprocess.Popen(cmd, **kwargs)
    with _ACTIVE_LOCK:
        _ACTIVE.add(proc)
    try:
        out, err = proc.communicate()
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    except BaseException:  # Ctrl-C / SIGTERM: never leave the child running
        proc.kill()
        proc.wait()
        raise
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(proc)


def _draw_progress(label: str, secs: float | None, total: float | None, extra: str = "") -> None:
    if secs is None:
        line = f"{BLUE}{label}{RESET} {DIM}…{RESET}"
    elif total:
        frac = min(secs / total, 1.0)
        n = int(frac * 24)
        suffix = f"  {DIM}{extra}{RESET}" if extra else ""
        line = (f"{BLUE}{label}{RESET} {PURPLE}{'█' * n}{DIM}{'░' * (24 - n)}{RESET} "
                f"{frac * 100:5.1f}%{suffix}")
    else:
        line = f"{BLUE}{label}{RESET} {secs:6.1f}s"
    sys.stderr.write("\r" + line)
    sys.stderr.flush()


def _clear_progress() -> None:
    if SHOW_PROGRESS:
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()


def _echo_cmd(cmd: list[str]) -> None:
    if VERBOSE:
        _clear_progress()
        _emit(f"{DIM}$ {shlex.join(cmd)}{RESET}")


def run_ffmpeg(cmd: list[str], label: str, total_secs: float | None, dry_run: bool) -> None:
    if dry_run:
        _emit(shlex.join(cmd), stream=sys.stdout)
        return
    _echo_cmd(cmd)
    if SHOW_PROGRESS:
        _draw_progress(label, None, None)

    errors: list[str] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)  # [fix] utf-8
    with _ACTIVE_LOCK:  # [fix] register for signal cleanup
        _ACTIVE.add(proc)
    try:
        assert proc.stdout is not None
        cur_t: float | None = None
        speed: float | None = None
        for raw in proc.stdout:
            line = raw.strip()
            m = KV_RE.match(line)
            if m and (m.group(1) in PROGRESS_KEYS or m.group(1).startswith("stream_")):
                key, val = m.group(1), m.group(2)
                if key in ("out_time_us", "out_time_ms"):
                    try:  # out_time_ms is (historically) microseconds too
                        cur_t = int(val) / 1_000_000
                    except ValueError:
                        pass  # "N/A" early in the run
                elif key == "speed":
                    try:
                        speed = float(val.rstrip("x")) or None
                    except ValueError:
                        speed = None
                if SHOW_PROGRESS and key in ("out_time_us", "out_time_ms"):
                    extra = ""
                    if speed:  # [new] show speed + ETA in the progress bar
                        extra = f"{speed:g}×"
                        if total_secs and cur_t is not None and cur_t < total_secs:
                            extra += f"  eta {max(0.0, (total_secs - cur_t) / speed):.0f}s"
                    _draw_progress(label, cur_t, total_secs, extra)
            elif line:
                errors.append(line)
                if VERBOSE:
                    _clear_progress()
                    _emit(f"{DIM}  {line}{RESET}")
        rc = proc.wait()
    finally:
        if proc.poll() is None:  # Ctrl-C / SIGTERM: never leave ffmpeg running
            proc.kill()
            proc.wait()
        with _ACTIVE_LOCK:
            _ACTIVE.discard(proc)
        _clear_progress()
    if rc != 0:
        die("FFmpeg failed:\n  " + "\n  ".join(errors[-15:]))


def run_plain(cmd: list[str], label: str, dry_run: bool, suffix: str = "") -> None:
    """Run a non-FFmpeg helper (gifsicle, gifski)."""
    if dry_run:
        _emit(shlex.join(cmd) + suffix, stream=sys.stdout)
        return
    _echo_cmd(cmd)
    if SHOW_PROGRESS:
        _draw_progress(label, None, None)
    try:
        r = _tracked_run(cmd, capture_output=True)  # [fix] tracked + utf-8
    finally:
        _clear_progress()
    if r.returncode != 0:
        die(f"{cmd[0]} failed:\n  " + "\n  ".join((r.stderr or r.stdout or "").strip().splitlines()[-15:]))
    if VERBOSE and r.stderr and r.stderr.strip():
        _emit(f"{DIM}  {r.stderr.strip()}{RESET}")


def decode_flags(args: argparse.Namespace) -> list[str]:
    """Input-side flags: hardware acceleration + decode thread cap. [new]"""
    flags: list[str] = []
    if args.hwaccel:
        flags += ["-hwaccel", args.hwaccel]
    if args.threads:
        flags += ["-threads", str(args.threads)]
    return flags


def ffmpeg_base(args: argparse.Namespace) -> list[str]:
    cmd = ["ffmpeg", "-v", "warning" if args.verbose else "error", "-nostats",
           "-progress", "pipe:1", *decode_flags(args)]
    if args.threads:  # [new] cap filtergraph + encoder threads too
        cmd += ["-filter_complex_threads", str(args.threads)]
    return cmd


# ── Encoders ──────────────────────────────────────────────────────────────────
def dither_spec(a: argparse.Namespace) -> str:
    preset = QUALITY[a.quality]
    name = a.dither or preset["dither"]
    if name == "bayer":
        scale = a.bayer_scale if a.bayer_scale is not None else preset["bayer_scale"]
        return f"bayer:bayer_scale={scale}"
    return name  # note: dither=none requires a reasonably recent FFmpeg (paletteuse 'none' choice)


def encode_gif_ffmpeg(job: Job, fps: float, width: int, palette: Path | None = None) -> Path:
    a = job.args
    colors = a.colors or QUALITY[a.quality]["colors"]
    graph = build_graph(job, width, fps)
    palette = palette or (job.workdir / "palette.png")
    out = job.workdir / "out.gif"
    tf = time_flags(a)

    # [fix/enh] --max-size reuses the palette across width attempts at the same fps
    if palette.exists() and not a.dry_run:
        info(f"Step 1/2 — reusing palette from the previous attempt  ({colors} colors)")
    else:
        info(f"Step 1/2 — generating palette  (fps={fps:g}, width={width}px, {colors} colors, mode={a.stats_mode})")
        run_ffmpeg([*ffmpeg_base(a), *tf, "-i", str(job.inp), "-filter_complex",
                    f"{graph};[g]palettegen=max_colors={colors}:stats_mode={a.stats_mode}[p]",
                    "-map", "[p]", "-update", "1", "-y", str(palette)],
                   "palette", job.out_len, a.dry_run)

    use = f"paletteuse=dither={dither_spec(a)}"
    if a.diff_mode == "rectangle":
        use += ":diff_mode=rectangle"
    info("Step 2/2 — rendering GIF")
    run_ffmpeg([*ffmpeg_base(a), *tf, "-i", str(job.inp), "-i", str(palette),
                "-filter_complex", f"{graph};[g][1:v]{use}[o]",
                # GIF muxer takes the NETSCAPE loop count verbatim (see plays())
                "-map", "[o]", "-loop", str(a.loop), "-f", "gif", "-y", str(out)],
               "render ", job.out_len, a.dry_run)
    return out


def encode_gif_gifski(job: Job, fps: float, width: int) -> Path:
    a = job.args
    graph = build_graph(job, width, fps)
    frames = job.workdir / "frames"
    out = job.workdir / "out.gif"
    if not a.dry_run:
        shutil.rmtree(frames, ignore_errors=True)
        frames.mkdir()

    info(f"Step 1/2 — extracting frames  (fps={fps:g}, width={width}px)")
    run_ffmpeg([*ffmpeg_base(a), *time_flags(a), "-i", str(job.inp), "-filter_complex", graph,
                "-map", "[g]", "-f", "image2", "-start_number", "0", str(frames / "%06d.png")],
               "frames ", job.out_len, a.dry_run)

    info("Step 2/2 — encoding with gifski")
    cmd = ["gifski", "--quiet", "--fps", f"{fps:g}", "--quality", str(QUALITY[a.quality]["gifski"]),
           "--repeat", str(a.loop), "-o", str(out)]
    if a.dry_run:
        run_plain(cmd, "gifski", True, suffix=f" {shlex.quote(str(frames))}/*.png")
    else:
        files = sorted(str(p) for p in frames.glob("*.png"))
        if not files:
            die("FFmpeg produced no frames (is the clip range valid?)")
        if sum(len(f) + 3 for f in files) > 1_000_000:  # [new] ARG_MAX headroom warning
            warn("Very long clip — the gifski command line is huge; if it fails with "
                 "'argument list too long', lower --fps or split the clip.")
        run_plain([*cmd, *files], "gifski ", False)
        shutil.rmtree(frames, ignore_errors=True)
    return out


def encode_single_pass(job: Job, fps: float, width: int) -> Path:
    a = job.args
    q = QUALITY[a.quality]
    out = job.workdir / f"out{EXT[job.fmt]}"
    graph = build_graph(job, width, fps)
    if job.fmt == "webp":
        codec = ["-c:v", "libwebp_anim", "-lossless", "0", "-q:v", str(q["webp"]),
                 "-compression_level", "6", "-loop", str(plays(a.loop)), "-f", "webp"]
    elif job.fmt == "apng":
        codec = ["-c:v", "apng", "-plays", str(plays(a.loop)), "-f", "apng"]
    else:  # mp4
        codec = ["-c:v", "libx264", "-preset", a.x264_preset, "-crf", str(q["crf"]),  # [new] preset
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-f", "mp4"]
    if a.threads:  # output-side cap (the input-side -threads only limits decoding)
        codec += ["-threads", str(a.threads)]
    audio: list[str] = []
    if job.fmt == "mp4" and a.audio:  # [new] optional soundtrack
        audio = ["-map", "0:a?", "-c:a", "aac", "-b:a", "128k", "-shortest"]
    info(f"Encoding {job.fmt.upper()}  (fps={fps:g}, width={width}px)")
    run_ffmpeg([*ffmpeg_base(a), *time_flags(a), "-i", str(job.inp), "-filter_complex", graph,
                "-map", "[g]", *audio, *codec, "-y", str(out)],
               "render ", job.out_len, a.dry_run)
    return out


def run_gifsicle(job: Job, gif: Path) -> Path:
    out = job.workdir / "optimized.gif"
    cmd = ["gifsicle", f"-O{job.optimize}"]
    if job.lossy:
        cmd.append(f"--lossy={job.lossy}")
    cmd += [str(gif), "-o", str(out)]
    info(f"Optimizing with gifsicle (-O{job.optimize}" + (f", lossy={job.lossy}" if job.lossy else "") + ")")
    run_plain(cmd, "gifsicle", job.args.dry_run)
    return out


def encode(job: Job, fps: float, width: int, *, optimize: bool | None = None,
           palette: Path | None = None) -> Path:
    do_opt = job.optimize if optimize is None else optimize
    if job.fmt == "gif":
        if job.args.engine == "gifski":
            out = encode_gif_gifski(job, fps, width)
        else:
            out = encode_gif_ffmpeg(job, fps, width, palette=palette)
        if do_opt:
            out = run_gifsicle(job, out)
        return out
    return encode_single_pass(job, fps, width)


def fit_to_size(job: Job) -> Path:
    """Find the widest output that fits --max-size.

    Size grows roughly with width², so the first miss jumps straight to an
    estimated width; once a fit exists we interpolate inside the (fits, too big)
    bracket. If even MIN_WIDTH is too big, fps is lowered and the width search
    restarts from the requested width. [fix]

    Speed-ups: the palette is reused across width attempts at the same fps, and
    gifsicle runs once at the very end — so the sizes measured during the search
    are upper bounds, which is conservative (safe) for the search. [new]
    """
    a = job.args
    target = a.max_size
    ext = EXT[job.fmt]
    fps, width = a.fps, job.width
    lo = hi = None            # widest width that fits / narrowest that doesn't (at current fps)
    lo_size = 0
    best: Path | None = None
    best_w = 0
    smallest: Path | None = None
    smallest_size = 0
    reuse: Path | None = None          # [new] palette reuse across width attempts
    reuse_fps: float | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        pal = reuse if reuse_fps == fps else None
        out = encode(job, fps, width, optimize=False, palette=pal)
        if job.fmt == "gif" and a.engine == "ffmpeg" and not a.dry_run:
            reuse, reuse_fps = job.workdir / "palette.png", fps
        if a.dry_run:
            if job.optimize:
                run_gifsicle(job, out)  # print the command only
            info("(dry-run: skipping size search)")
            return out
        size = out.stat().st_size
        if smallest is None or size < smallest_size:
            smallest = job.workdir / f"smallest{ext}"   # copy: out{ext} is overwritten by the next attempt
            shutil.copy2(out, smallest)
            smallest_size = size

        if size <= target:
            info(f"Size {human(size)} fits {human(target)} ✓  ({width}px @ {fps:g}fps)")
            if best is None or width > best_w:
                best = job.workdir / f"best{ext}"
                shutil.copy2(out, best)
                best_w = width
            lo, lo_size = width, size
            if hi is None:
                break  # fit at the requested settings (or nothing bigger left to try)
        else:
            warn(f"Size {human(size)} exceeds {human(target)}  ({width}px @ {fps:g}fps)")
            hi = width

        if attempt == MAX_ATTEMPTS:
            break
        if lo is not None and hi is not None:
            if hi - lo <= max(8, int(hi * 0.04)):
                break  # close enough to the largest fitting width
            est = int(lo * math.sqrt(target / lo_size) * 0.98)
            nxt = min(max(est, lo + 4), hi - 4)
            if nxt <= lo or nxt >= hi:
                break
            info(f"  → trying {nxt}px")
        elif width > MIN_WIDTH:
            nxt = max(MIN_WIDTH, min(int(width * math.sqrt(target / size) * 0.95), width - 8))
            info(f"  → estimating {nxt}px")
        elif fps > MIN_FPS:
            fps = max(MIN_FPS, round(fps * 0.7, 1))
            lo = hi = None
            nxt = job.width        # [fix] re-climb from the requested width (was stuck at MIN_WIDTH)
            reuse_fps = None       # [new] new frame rate -> regenerate the palette
            (job.workdir / "palette.png").unlink(missing_ok=True)  # else encode_gif_ffmpeg sees it and reuses it
            info(f"  → at minimum width; reducing fps to {fps:g} and restarting the width search")
        else:
            warn("Reached minimum width and fps.")
            break
        width = nxt

    chosen = best if best is not None else smallest
    assert chosen is not None
    if best is None:
        warn(f"Could not get below {human(target)}; keeping the smallest attempt ({human(smallest_size)}).")
    if job.optimize:  # [new] gifsicle deferred to the end (one run, on the winner)
        chosen = run_gifsicle(job, chosen)
    return chosen


# ── Input handling ────────────────────────────────────────────────────────────
def resolve_input(spec: str, workdir: Path, dry_run: bool = False) -> tuple[Path, str, Path]:
    """-> (readable file, default output stem, default output directory)."""
    if dry_run and (spec == "-" or URL_RE.match(spec)):
        # --dry-run must not consume stdin or download anything
        if spec == "-":
            return workdir / "stdin_input", "stdin", Path.cwd()
        name = Path(spec.split("?")[0].rstrip("/")).name or "download"
        return workdir / "downloaded_video", Path(name).stem or "download", Path.cwd()
    if spec == "-":
        dest = workdir / "stdin_input"
        with open(dest, "wb") as f:
            shutil.copyfileobj(sys.stdin.buffer, f)
        if dest.stat().st_size == 0:
            die("No data received on stdin.")
        return dest, "stdin", Path.cwd()
    if URL_RE.match(spec):
        ytdlp = shutil.which("yt-dlp")
        if not ytdlp:
            die("yt-dlp is required for URL input (sudo dnf install yt-dlp  or  pip install yt-dlp).")
        info(f"Downloading via yt-dlp: {spec}")
        r = _tracked_run(  # [fix] tracked + utf-8 (was subprocess.run)
            [ytdlp, "--no-playlist", "--restrict-filenames", "--quiet", "--no-warnings",
             "-f", "bv*[height<=1080]/b[height<=1080]/b",
             "-o", str(workdir / "%(title).80B.%(ext)s"),
             "--print", "after_move:filepath", "--no-simulate", spec],
            capture_output=True)
        lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
        if r.returncode != 0 or not lines or not Path(lines[-1]).is_file():
            die("yt-dlp failed:\n  " + "\n  ".join((r.stderr or r.stdout or "").strip().splitlines()[-8:]))
        p = Path(lines[-1])
        return p, p.stem, Path.cwd()
    p = Path(spec)
    if not p.is_file():
        die(f"Input file not found: {p}")
    return p, p.stem, p.parent


def pick_format(args: argparse.Namespace) -> str:
    if args.format:
        return args.format
    if args.output:
        return SUFFIX_FORMAT.get(Path(args.output).suffix.lower(), "gif")
    return "gif"


def _cropdetect_window(inp: Path, start: float, dur: float,
                       args: argparse.Namespace) -> tuple[int, int, int, int] | None:
    cmd = ["ffmpeg", "-hide_banner", "-nostats", *decode_flags(args),
           "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(inp),
           "-vf", "fps=2,cropdetect=limit=24:round=2:reset=0", "-an", "-f", "null", "-"]
    r = _tracked_run(cmd, capture_output=True)  # [fix] tracked + utf-8
    found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", r.stderr or "")
    if not found:
        return None
    w, h, x, y = map(int, found[-1])  # reset=0 -> the last value is the cumulative bounding box
    return w, h, x, y


def detect_crop(inp: Path, src: Source, args: argparse.Namespace) -> tuple[int, int, int, int] | None:
    """Run cropdetect over up to two 10s windows (clip start + middle) and union
    the boxes — the first 10s alone often contains letterboxed intros/logos. [new]"""
    boxes = []
    first = _cropdetect_window(inp, args.start, min(args.duration or 10.0, 10.0), args)
    if first:
        boxes.append(first)
    avail = (src.duration - args.start) if src.duration else None
    span = min(args.duration, avail) if (args.duration and avail is not None) else (args.duration or avail)
    if span and span > 20:
        mid = args.start + max(0.0, span / 2 - 5)
        second = _cropdetect_window(inp, mid, min(10.0, args.start + span - mid), args)
        if second:
            boxes.append(second)
    if not boxes:
        return None
    x = min(b[2] for b in boxes)
    y = min(b[3] for b in boxes)
    right = max(b[0] + b[2] for b in boxes)
    bottom = max(b[1] + b[3] for b in boxes)
    w, h = right - x, bottom - y
    w -= w % 2  # keep cropdetect's even-dimension guarantee for the union too
    h -= h % 2
    if (w, h, x, y) == (src.width, src.height, 0, 0) or w <= 0 or h <= 0:
        return None
    return w, h, x, y


def warn_noops(args: argparse.Namespace, fmt: str) -> None:
    """[new] Flag options that will silently do nothing for this output format."""
    if fmt != "gif":
        for name, val in (("colors", args.colors), ("dither", args.dither),
                          ("bayer-scale", args.bayer_scale)):
            if val is not None:
                warn(f"--{name} only applies to GIF output; ignoring.")
        if args.stats_mode != "diff":
            warn("--stats-mode only applies to GIF output; ignoring.")
        if args.diff_mode != "none":
            warn("--diff-mode only applies to GIF output; ignoring.")
    elif args.engine == "gifski":
        for name, val in (("colors", args.colors), ("dither", args.dither),
                          ("bayer-scale", args.bayer_scale)):
            if val is not None:
                warn(f"--{name} is ignored by the gifski engine.")
        if args.stats_mode != "diff":
            warn("--stats-mode is ignored by the gifski engine.")
        if args.diff_mode != "none":
            warn("--diff-mode is ignored by the gifski engine.")
    else:
        eff = args.dither or QUALITY[args.quality]["dither"]
        if args.bayer_scale is not None and eff != "bayer":
            warn(f"--bayer-scale is ignored (dither is '{eff}', not bayer).")
    if args.audio:
        if fmt != "mp4":
            warn("--audio only applies to MP4 output; ignoring.")
        elif args.speed != 1.0 or args.reverse or args.boomerang or args.fade:
            warn("--audio: the soundtrack is copied as-is; "
                 "speed/reverse/boomerang/fade affect video only.")


def confirm_overwrite(path: Path, args: argparse.Namespace) -> None:
    if not path.exists() or args.force or args.dry_run:
        return
    if not INTERACTIVE:
        die(f"'{path}' already exists (use --force to overwrite).")
    with _LOCK:
        sys.stderr.write(f"{YELLOW}[WARN] {RESET} '{path}' already exists. Overwrite? [y/N] ")
        sys.stderr.flush()
    if input().strip().lower() != "y":
        raise Skipped()


def copy_to_clipboard(text: str) -> bool:
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"], ["pbcopy"]):
        if shutil.which(cmd[0]):
            try:
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
            except OSError:
                continue  # [fix] tool present but broken — fall through to the next candidate
            p.communicate(text)
            if p.returncode == 0:  # [fix] e.g. wl-copy with no Wayland session used to "succeed"
                return True
    return False


def open_file(path: Path) -> bool:
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
        return True
    opener = shutil.which("open" if sys.platform == "darwin" else "xdg-open")
    if not opener:
        return False
    subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    return True


# ── One input -> one output ───────────────────────────────────────────────────
def describe(job: Job, output: Path) -> None:
    a = job.args
    info(f"Input:    {job.inp.name}  ({job.src.width}×{job.src.height})")
    info(f"Output:   {output}  [{job.fmt}]")
    info(f"FPS:      {a.fps:g}")
    info(f"Width:    {job.width}px")
    if job.fmt == "gif":
        info(f"Quality:  {a.quality} ({a.colors or QUALITY[a.quality]['colors']} colors, "
             f"dither={dither_spec(a)}, engine={a.engine})")
    else:
        extra = f" (x264 preset={a.x264_preset})" if job.fmt == "mp4" else ""  # [new]
        info(f"Quality:  {a.quality}{extra}")
    if a.audio and job.fmt == "mp4":  # [new]
        info("Audio:    yes (AAC copy, not speed/fade-adjusted)")
    if a.hwaccel:  # [new]
        info(f"HW accel: {a.hwaccel} (decode)")
    if a.threads:  # [new]
        info(f"Threads:  {a.threads}")
    if job.crop:
        info("Crop:     {}x{}+{}+{}{}".format(*job.crop, "  (auto)" if a.auto_crop else ""))
    if a.speed != 1.0:
        info(f"Speed:    {a.speed:g}×")
    if a.reverse:
        info("Reverse:  yes")
    if a.boomerang:
        info("Boomerang: yes")
    if a.fade:
        info(f"Fade:     {a.fade:g}s crossfade loop")
    if a.text:
        info(f"Caption:  {a.text!r} ({a.text_pos})")
    if a.start:
        info(f"Start:    {a.start:g}s")
    if a.duration:
        info(f"Duration: {a.duration:g}s")
    if a.max_size:
        info(f"Max size: {human(a.max_size)}")
    if job.optimize:
        info(f"gifsicle: -O{job.optimize}" + (f" --lossy={job.lossy}" if job.lossy else ""))
    if a.dry_run:
        warn("Dry-run mode — no files will be written.")
    _emit()


def process(spec: str, args: argparse.Namespace, batch: bool) -> Path | None:
    """Convert one input. Returns the output path (None for --dry-run)."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="video_to_gif_") as tmp:
        workdir = Path(tmp)
        inp, stem, default_dir = resolve_input(spec, workdir, args.dry_run)
        if args.dry_run and not inp.exists():
            warn("Dry-run: input not fetched; planning with a placeholder 1920x1080 source.")
            src = Source(1920, 1080, None)
        else:
            src = probe(inp)
        fmt = pick_format(args)
        warn_noops(args, fmt)  # [new] surface silent no-ops early
        # [fix] fail here instead of deep inside palettegen if --start is past the end
        if src.duration is not None and args.start > 0 and args.start >= src.duration:
            die(f"--start {args.start:g}s is at/after the end of this input ({src.duration:.1f}s)")
        out_dir = Path(args.out_dir) if args.out_dir else default_dir
        output = Path(args.output) if args.output else out_dir / f"{stem}{EXT[fmt]}"
        if args.output and not output.suffix:  # [fix] '-o out' used to produce a file named 'out'
            output = output.with_name(output.name + EXT[fmt])
        if output.resolve() == inp.resolve():
            die("Output path is the same as the input file.")

        # Capability checks up front, so we fail before doing any work.
        if fmt == "gif" and args.engine == "gifski" and not shutil.which("gifski"):
            die("gifski is not installed (cargo install gifski, or use --engine ffmpeg).")
        if fmt == "webp":
            require_ffmpeg("encoders", "libwebp_anim", "WebP output")
        if fmt == "mp4":
            require_ffmpeg("encoders", "libx264", "MP4 output")
        if fmt == "mp4" and args.audio:  # [new]
            require_ffmpeg("encoders", "aac", "--audio")
        if args.text:
            require_ffmpeg("filters", "drawtext", "--text")
        if args.fade:
            require_ffmpeg("filters", "xfade", "--fade")
        optimize, lossy = args.optimize, args.lossy
        if optimize and fmt != "gif":
            warn("-O/--lossy only apply to GIF output; ignoring.")
            optimize = lossy = None
        elif optimize and not shutil.which("gifsicle"):
            warn("gifsicle not found; skipping -O/--lossy (sudo dnf install gifsicle).")
            optimize = lossy = None

        crop = args.crop
        if args.auto_crop and not inp.exists():
            warn("Dry-run: skipping --auto-crop (input not fetched).")
        elif args.auto_crop:
            crop = detect_crop(inp, src, args)
            info("Auto-crop detected {}x{}+{}+{}".format(*crop) if crop else "Auto-crop: no black bars found.")

        base_w = crop[0] if crop else src.width
        width = min(args.width, base_w) if args.width > 0 else base_w
        avail = (src.duration - args.start) if src.duration else None
        cands = [v for v in (args.duration, avail) if v is not None and v > 0]
        clip_len = min(cands) if cands else None
        base_len = clip_len / args.speed if clip_len else None
        out_len = base_len
        if base_len and args.boomerang:
            out_len = base_len * 2
        elif base_len and args.fade:
            out_len = base_len - args.fade

        if args.fade:
            if base_len is None:
                die("--fade needs a known clip length; pass --duration.")
            if base_len <= 2 * args.fade + 1 / args.fps:
                die(f"Clip is too short ({base_len:.1f}s) for a {args.fade:g}s crossfade; "
                    "use a shorter --fade or a longer clip.")

        textfile = None
        if args.text:
            textfile = workdir / "caption.txt"
            textfile.write_text(args.text.replace("\\n", "\n"), encoding="utf-8")

        job = Job(args, inp, src, fmt, workdir, crop, width, base_len, out_len,
                  textfile, optimize, lossy)

        if base_len and args.boomerang and round(base_len * args.fps) < 3:
            die(f"Clip too short for --boomerang ({base_len:.2f}s @ {args.fps:g}fps is under 3 frames); "
                "use a longer clip or a higher --fps.")
        if base_len and (args.reverse or args.boomerang):
            est = base_len * args.fps * width * job.out_height(width) * 1.5
            if est > RAM_WARN_BYTES:
                warn(f"reverse/boomerang buffers every frame in RAM (~{human(est)} here); "
                     "consider a shorter clip, lower --fps or smaller --width.")

        if batch:
            info(f"{fmt.upper()} → {output}")
        else:
            describe(job, output)

        # Preview mode: one frame, crop + scale (+ caption)
        if args.preview:
            preview = output.with_name(f"{output.stem}_preview.png")
            confirm_overwrite(preview, args)
            vf = []
            if crop:
                vf.append("crop={}:{}:{}:{}".format(*crop))
            vf.append(f"scale=w={width}:h=-2:flags=lanczos")
            if args.text:
                vf.append(drawtext_filter(job, width))
            info(f"Preview — extracting one frame at {args.start:g}s...")
            if not args.dry_run:
                preview.parent.mkdir(parents=True, exist_ok=True)
            run_ffmpeg(["ffmpeg", "-v", "error", *decode_flags(args),  # [new] hwaccel for preview
                        "-ss", f"{args.start:.3f}", "-i", str(inp),
                        "-vf", ",".join(vf), "-frames:v", "1", "-y", str(preview)],
                       "preview", None, args.dry_run)
            if args.dry_run:
                return None
            p = probe(preview)
            success(f"Preview saved: {preview}  ({p.width}×{p.height} px)")
            return preview

        confirm_overwrite(output, args)

        result = fit_to_size(job) if args.max_size else encode(job, args.fps, width)
        if args.dry_run:
            _emit()
            success("Dry-run complete — no files written.")
            return None
        if not result.exists():
            die("Conversion failed — output file was not created.")
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(result), str(output))  # only touch the real output after full success
        src_size = inp.stat().st_size if inp.exists() else None

    size = output.stat().st_size
    try:
        d = probe(output)
        if not (d.width and d.height):  # ffprobe reports 0x0 for animated WebP
            raise ConvError("no dimensions")
        dims = f"{d.width}×{d.height}"
    except ConvError:
        dims = f"{width}×{job.out_height(width)}"
    success(f"Created {output.name}")
    if not QUIET:  # [new] quiet keeps stdout paths + warnings/errors only
        _emit(f"  {BOLD}File:{RESET}       {output}")
        _emit(f"  {BOLD}Size:{RESET}       {human(size)}")
        _emit(f"  {BOLD}Dimensions:{RESET} {dims} px")
        if spec != "-" and not URL_RE.match(spec) and src_size:
            _emit(f"  {BOLD}Source:{RESET}     {human(src_size)} → {human(size)} "
                  f"({size / src_size * 100:.0f}% of original)")
        _emit(f"  {BOLD}Time:{RESET}       {time.monotonic() - started:.1f}s\n")
    return output


def run_one(spec: str, args: argparse.Namespace, batch: bool) -> tuple[str, Path | None]:
    if batch:
        _tls.tag = f"{DIM}[{Path(spec).name if spec != '-' else 'stdin'}]{RESET} "
    try:
        out = process(spec, args, batch)
        return ("dry" if out is None else "ok"), out
    except Skipped:
        info("Skipped.")
        return "skipped", None
    except ConvError as e:
        fail(str(e))
        return "failed", None
    except Exception as e:  # [fix] one bad job must never kill the batch with a traceback
        if VERBOSE:
            traceback.print_exc()
        fail(f"Unexpected error: {type(e).__name__}: {e}")
        return "failed", None


# ── Main ──────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    global VERBOSE, SHOW_PROGRESS, INTERACTIVE, QUIET
    if sys.platform == "win32":
        os.system("")  # [fix] enable ANSI escape sequences on legacy Windows consoles
    signal.signal(signal.SIGTERM, _on_sigterm)  # [fix] kill children, let cleanup run
    try:
        args = parse_args(argv)
    except ConvError as e:
        fail(str(e))
        return 1

    VERBOSE = args.verbose
    QUIET = args.quiet
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            fail(f"{tool} is not installed or not in PATH.\n"
                 "  Fedora: sudo dnf install ffmpeg   Ubuntu: sudo apt install ffmpeg   "
                 "macOS: brew install ffmpeg")
            return 1

    inputs: list[str] = args.input
    batch = len(inputs) > 1
    jobs = min(args.jobs, len(inputs))
    SHOW_PROGRESS = sys.stderr.isatty() and jobs == 1 and not QUIET
    INTERACTIVE = sys.stdin.isatty() and jobs == 1

    if args.output and args.out_dir:  # [new] no more silent ignoring
        warn("--out-dir is ignored when --output is given.")
    if batch and sum(1 for s in inputs if URL_RE.match(s)) > 1:  # [new]
        warn("Multiple URL inputs: same-titled videos may collide on the same output name.")

    if not QUIET:
        _emit(f"\n{BOLD}{'━' * 50}{RESET}\n{BOLD}  Video → GIF Converter{RESET}\n{BOLD}{'━' * 50}{RESET}\n")
    if batch:
        info(f"Batch: {len(inputs)} inputs, {jobs} job(s) in parallel")

    results: list[tuple[str, str, Path | None]] = []  # (spec, status, path)
    try:
        if jobs == 1:
            for spec in inputs:
                st, path = run_one(spec, args, batch)
                results.append((spec, st, path))
                if path:
                    _emit(str(path), stream=sys.stdout)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
                futs = {pool.submit(run_one, spec, args, True): spec for spec in inputs}
                try:
                    for fut in concurrent.futures.as_completed(futs):
                        st, path = fut.result()
                        results.append((futs[fut], st, path))
                        if path:
                            _emit(str(path), stream=sys.stdout)
                except (KeyboardInterrupt, SystemExit):  # [fix] SystemExit too (SIGTERM)
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
    except KeyboardInterrupt:
        _kill_active()
        _emit()
        warn("Interrupted.")
        return 130
    except SystemExit:
        _kill_active()
        _emit()
        warn("Terminated.")
        return 143

    _tls.tag = ""  # sequential batches leave the last input's tag on the main thread
    outputs = [p for _, st, p in results if st == "ok" and p]
    failed_specs = [s for s, st, _ in results if st == "failed"]
    failed = len(failed_specs)
    if batch:
        ok = sum(1 for _, st, _ in results if st in ("ok", "dry"))
        skipped = len(results) - ok - failed
        (warn if failed else success)(f"Batch finished: {ok} succeeded, {failed} failed, "
                                      f"{skipped} skipped.")
        if failed_specs:  # [new] easy re-run of just the failures
            warn("Failed inputs: " + ", ".join(failed_specs))
    if outputs and args.copy:
        if copy_to_clipboard("\n".join(map(str, outputs))):
            info("Copied path to clipboard." if len(outputs) == 1 else "Copied paths to clipboard.")
        else:
            warn("No clipboard tool found (wl-copy, xclip, xsel, pbcopy).")
    if outputs and args.open:
        for p in outputs:
            if not open_file(p):
                warn("No opener found (xdg-open / open).")
                break
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
