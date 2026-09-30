<div align="center">

# 🎬 video_to_gif

**Turn any video — file, URL, or stdin — into a polished, loop-perfect GIF
(or WebP / APNG / MP4) with a single command.**

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FFmpeg](https://img.shields.io/badge/FFmpeg-required-007808?logo=ffmpeg&logoColor=white)](https://ffmpeg.org/)
[![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey)](#requirements)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

*Two-pass palette GIFs · seamless loops · smart size targeting · batch mode*

</div>

---

<!-- Optional: swap in a real demo GIF once you have one
<p align="center">
  <img src="docs/demo.gif" alt="video_to_gif demo" width="700">
</p>
-->

## ✨ Why

Most "video → GIF" one-liners produce 20 MB of muddy, stuttering pixels.
`video_to_gif.py` does the things you'd otherwise script by hand:

- 🎨 **Two-pass palette encoding** — `palettegen` → `paletteuse` for dramatically better colors than single-pass conversion, with full control over palette size and dithering
- 🔁 **Loop-perfect effects** — `--boomerang` (with pivot-frame de-duplication), `--fade` (crossfades the clip's end into its start), `--reverse`, `--speed`
- 🎯 **`--max-size` targeting** — tell it *"fit under 10 MB"* and it searches width (then fps) to land on the **widest result that fits** — perfect for Discord, Slack, and forum upload limits
- ✂️ **Framing** — manual crop, automatic black-bar detection (`cropdetect`), captions with `--text`, or keep the source's exact size and frame rate with `--match-size` / `--match-fps`
- 🚀 **Fast workflows** — batch conversion with parallel jobs (`-j`), stdin/pipe input, URL input via [yt-dlp](https://github.com/yt-dlp/yt-dlp), `--preview` frame extraction, `--dry-run`
- 🧼 **Clean pipes** — logs go to stderr, only result paths go to stdout, so it composes safely in shell pipelines
- ⚙️ **Configurable** — your favorite defaults in `~/.config/video_to_gif/config.toml`
- 📦 **Multiple output formats** — animated GIF, WebP, APNG, or MP4 (x264, optional audio)

---

## 🔧 Requirements

| | Requirement | What it unlocks |
|---|---|---|
| ✅ | **Python 3.11+** | The script itself (3.11+ needed for `tomllib` config support) |
| ✅ | **FFmpeg + ffprobe** | All conversions |
| 🔹 | [gifsicle](https://www.lcdf.org/gifsicle/) | `-O` / `--lossy` GIF post-optimization |
| 🔹 | [gifski](https://gif.ski/) | `--engine gifski` (higher-quality GIF encoder) |
| 🔹 | [yt-dlp](https://github.com/yt-dlp/yt-dlp) | URL input (`https://…`) |
| 🔹 | `wl-copy` / `xclip` / `xsel` / `pbcopy` | `--copy` (clipboard) |
| 🔹 | `xdg-open` / `open` | `--open` (auto-open result) |

## 📥 Installation

```bash
# 1. Install FFmpeg (pick your platform)
sudo dnf install ffmpeg      # Fedora / RHEL
sudo apt install ffmpeg      # Debian / Ubuntu
brew install ffmpeg          # macOS
# Windows: winget install Gyan.FFmpeg   (or scoop install ffmpeg)

# 2. Grab the script
curl -LO https://raw.githubusercontent.com/Lunatic16/video_to_gif/main/video_to_gif.py
chmod +x video_to_gif.py
sudo mv video_to_gif.py /usr/local/bin/video_to_gif   # optional: put it on PATH

# 3. Optional extras
sudo dnf install gifsicle yt-dlp        # Fedora
brew install gifsicle yt-dlp            # macOS
```

Verify everything:

```bash
video_to_gif --version
```

## 🚀 Quick Start

```bash
# Simplest case: defaults (480px, 15 fps, medium quality)
video_to_gif clip.mp4

# A classic trim: 5s in, 3 seconds long, 640px wide, high quality
video_to_gif -s 5 -d 3 -w 640 -q high clip.mp4 -o demo.gif

# Caption + seamless crossfade loop
video_to_gif --fade 0.5 --text "when the build passes ✅" clip.mp4

# Ship it: boomerang loop, gifsicle-optimized, under 5 MB
video_to_gif --boomerang --max-size 5M -O clip.mp4

# Keep the source's exact dimensions and frame rate
video_to_gif --match-size --match-fps --webp clip.mp4

# Different formats
video_to_gif --webp -q high clip.mp4      # modern, much smaller
video_to_gif --mp4 --audio clip.mp4       # x264 with sound

# Preview the first frame before committing to a long encode
video_to_gif --preview -s 12 clip.mp4

# Batch: convert everything in parallel, into gifs/
video_to_gif -j 4 --out-dir gifs/ *.mp4

# URLs and pipes work too
video_to_gif https://example.com/watch?v=abc -s 12 -d 4
cat clip.mp4 | video_to_gif - -o out.gif
```

## 🍳 Cookbook Recipes

<details open>
<summary><b>Discord / Slack upload limit</b></summary>

```bash
video_to_gif --max-size 10M clip.mp4 -o discord.gif
```
Finds the widest resolution that stays under 10 MB. (Tip: `--webp` gets you
the same content at a fraction of the size — but check whether the target
platform accepts animated WebP.)
</details>

<details>
<summary><b>Slack emoji / reaction</b></summary>

```bash
video_to_gif -c 480x480+220+160 -r 12 -w 128 -q high -O clip.mp4 -o emoji.gif
```
Crop a square region, drop the fps, tiny width, full optimization.
</details>

<details>
<summary><b>Seamless "living wallpaper" loop</b></summary>

```bash
video_to_gif --fade 0.8 --webp -w 800 -r 24 clip.mp4 -o loop.webp
```
The last 0.8 s crossfades into the start — no visible loop point.
</details>

<details>
<summary><b>Code demo with caption</b></summary>

```bash
video_to_gif -s 0 -d 8 -w 720 --text "before / after" --text-pos top demo.mp4
```
Multi-line captions: use <code>\n</code> — <code>--text "line one\nline two"</code>.
</details>

<details>
<summary><b>Trim black bars automatically</b></summary>

```bash
video_to_gif --auto-crop --max-size 3M screencast.mkv
```
</details>

<details>
<summary><b>Keep the original size and frame rate</b></summary>

```bash
video_to_gif --match-size --match-fps --webp clip.mp4
```
No downscaling and the source's own fps (e.g. 23.976 or 30). Works best with
`--webp`, `--apng` or `--mp4`; for GIF see the
[timing note](#-faq) below. Add `--max-size` and the tool may still shrink the
result to fit.
</details>

<details>
<summary><b>Drop into a shell pipeline</b></summary>

```bash
video_to_gif clip.mp4 -o out.gif | xclip -selection clipboard   # path on clipboard
video_to_gif clip.mp4 | while read f; do mv "$f" ~/gifs/; done  # stdout = paths only
```
</details>

## 📖 Options Reference

<details>
<summary><b>Output & format</b></summary>

| Option | Description |
|---|---|
| `-o, --output FILE` | Output file (single input only) |
| `--out-dir DIR` | Write outputs into DIR (batch-friendly) |
| `-F, --format FMT` | `gif` \| `webp` \| `apng` \| `mp4` (default: from `-o` suffix — `.png` counts as APNG — else `gif`) |
| `--webp` / `--apng` / `--mp4` | Shortcuts for `-F …` |
| `--audio` | MP4 only: mux source audio as AAC 128k |
| `--engine` | `ffmpeg` (default) or `gifski` (GIF only) |
</details>

<details>
<summary><b>Clip trimming & geometry</b></summary>

| Option | Description |
|---|---|
| `-s, --start TIME` | Start time — seconds or `[HH:]MM:SS` |
| `-d, --duration TIME` | Duration |
| `-e, --end TIME` | End time (alternative to `-d`) |
| `-r, --fps FPS` | Frames per second (default 15) |
| `--match-fps` | Use the source video's frame rate; overrides `--fps` (and a config-file `fps`) |
| `-w, --width PX` | Output width; never upscales; `0`/`-1` = source width |
| `--match-size` | Keep the source's original dimensions (no scaling); overrides `--width`. With `--crop`/`--auto-crop` it keeps the cropped region at native resolution |
| `-c, --crop WxH+X+Y` | Crop region before scaling |
| `--auto-crop` | Detect and trim black bars |
| `-l, --loop N` | `0` = forever, `-1` = play once, `N` = N extra repeats (ignored for MP4) |
</details>

<details>
<summary><b>Motion & effects</b></summary>

| Option | Description |
|---|---|
| `-S, --speed FACTOR` | Playback speed (e.g. `2.0` = 2× faster) |
| `-R, --reverse` | Play backwards |
| `-b, --boomerang` | Forward + backward, deduplicated pivot frames (clip must have at least 3 frames) |
| `--fade SECS` | Seamless loop: crossfade end into start |
| `--text` / `--text-pos` / `--text-size` / `--font` | Caption overlay |
</details>

<details>
<summary><b>Quality & size</b></summary>

| Option | Description |
|---|---|
| `-q, --quality` | `low` \| `medium` \| `high` |
| `--colors N` | Override palette size (2–256, GIF) |
| `--dither NAME` | Override dither algorithm (see list below) |
| `--bayer-scale 0-5` | Bayer dither scale |
| `--stats-mode` / `--diff-mode` | palettegen/paletteuse tuning |
| `--x264-preset` | x264 speed/size preset for MP4 |
| `-O, --optimize [1-3]` | gifsicle post-optimization (default level 3; GIF only) |
| `--lossy N` | gifsicle lossy compression (implies `-O`; try 30–80) |
| `-m, --max-size SIZE` | Target max size (`5M`, `500K`, `300000`); must be > 0, K/M/G are binary (1M = 1024² bytes) |
</details>

<details>
<summary><b>Run control</b></summary>

| Option | Description |
|---|---|
| `-p, --preview` | Extract one PNG frame at `--start` and exit (saved as `<output>_preview.png`) |
| `-j, --jobs N` | Convert N inputs in parallel |
| `--hwaccel NAME` | Hardware-accelerated decoding (`auto`, `vaapi`, `cuda`…) |
| `--threads N` | Cap FFmpeg decode, filter and (MP4/WebP) encode threads (avoids oversubscription with `-j`) |
| `--open` / `--copy` | Open result / copy path to clipboard |
| `-v, --verbose` | Show FFmpeg commands and warnings |
| `-Q, --quiet` | Suppress INFO/OK logs and the progress bar; warnings and errors still go to stderr, stdout still carries result paths |
| `--force` | Overwrite without prompting |
| `--dry-run` | Print the exact commands, write nothing (stdin/URL inputs aren't fetched — see the [FAQ](#-faq)) |
| `--config FILE` / `--no-config` | Config file control |
| `--version` | Print the version and exit |
</details>

**Available dither algorithms:** `bayer`, `heckbert`, `floyd_steinberg`, `sierra2`, `sierra2_4a`, `sierra3`, `burkes`, `atkinson`, `none`

## 🎚️ Quality Presets

| Preset | Colors | Dither | WebP `q` | x264 CRF | gifski quality |
|---|---|---|---|---|---|
| `low` | 64 | bayer (scale 5) | 50 | 30 | 50 |
| `medium` *(default)* | 128 | bayer (scale 3) | 70 | 26 | 80 |
| `high` | 256 | floyd_steinberg | 90 | 20 | 100 |

## ⚙️ Configuration File

Set your favorite defaults once — the command line always wins.

```toml
# ~/.config/video_to_gif/config.toml
fps = 12
width = 640
quality = "high"
optimize = 3
out-dir = "/home/you/gifs"   # use an absolute path; "~" is not expanded
```

Every long option name works as a key (`out-dir` or `out_dir`); flags take
`true`/`false`. Values are validated with the same rules as the CLI —
choices, ranges and types, including `format` — and bad values fail with a
clear message. A few keys make no sense in a config and are ignored with a
warning: `input`, `output`, `config`, `no-config`, `preview`, `dry-run`.
Mutually exclusive options are still enforced (e.g. `reverse = true` together
with `boomerang = true` is rejected).

Reading the file needs Python 3.11+; older versions ignore it with a warning.
Use `--no-config` to bypass it entirely, or `--config other.toml` for a
different file.

## 🔬 How It Works

```
input ──► crop ──► speed (setpts) ──► fps ──► scale (lanczos)
      ──► [fade loop | reverse | boomerang] ──► [caption] ──► encode
```

- **GIF (ffmpeg engine):** a first pass runs `palettegen` over the *exact*
  filtered frames that will be encoded; a second pass applies the palette via
  `paletteuse` with your chosen dithering. Optional `gifsicle -O/--lossy`
  post-pass, or the `gifski` encoder instead.
- **`--max-size`:** output size grows roughly with width², so the tool does a
  proportional first guess, then bisects inside the (fits / too big) bracket —
  landing on the widest result under your limit (at most 8 encodes). If even
  120 px doesn't fit, it lowers fps, regenerates the palette, and searches
  again from your requested width. If nothing fits, it keeps the smallest
  attempt and warns. The palette is reused across width attempts at the same
  fps, and gifsicle runs once at the end on the winner, so sizes measured during
  the search are slight upper bounds.
- **`--boomerang`:** splits the stream, reverses a copy, and drops the pivot
  frames on both joins, so the loop point is invisible. The clip needs at
  least 3 frames.
- **`--fade`:** the first `--fade` seconds are overlaid onto the last
  `--fade` seconds with `xfade`, producing a perfect crossfade loop.
- **`--auto-crop`:** runs `cropdetect` over up to two 10 s windows (the start
  of the clip and its middle) and crops to the union of the detected boxes.
- **`--match-fps` / `--match-size`:** the source's frame rate (average rate, so
  variable-frame-rate clips are handled) and displayed dimensions are read with
  ffprobe for each input, so batches of mixed videos each keep their own values.
  With `--match-size` the output height is pinned as well, so odd sizes such as
  481×271 come out exactly (MP4 is the exception: x264 needs even dimensions,
  so it is rounded down and you get a warning). If `--max-size` is also given
  it still takes priority and may reduce width and fps to fit.
- **Rotated sources:** rotation metadata (e.g. portrait phone clips) is
  honored, so `--width`, `--crop` and `--auto-crop` work on the picture as it
  is displayed.

## 🔌 Piping & Exit Codes

**stdout** carries only the resulting file path(s) (or the commands under
`--dry-run`) — safe to pipe. **stderr** carries all logs, progress, and
warnings (color is disabled automatically when piped or when `NO_COLOR` is set).

| Exit code | Meaning |
|---|---|
| `0` | All inputs converted |
| `1` | One or more inputs failed, or a startup error (bad config file, FFmpeg missing) |
| `2` | Invalid command-line arguments |
| `130` | Interrupted (Ctrl-C) |
| `143` | Terminated (SIGTERM) |

**Output names:** by default `<input-stem>.<ext>` next to the input file;
stdin and URL inputs write to the current directory (`stdin.gif`, or the video
title for URLs). Override with `-o` / `--out-dir`. An existing file prompts
before being overwritten (or fails when not interactive) unless you pass
`--force`.

## ❓ FAQ

<details>
<summary><b>My GIF is huge. Why?</b></summary>

GIF is a 1987 format: 256 colors, no inter-frame compression beyond delta
frames. For the same visual quality, prefer `--webp` (typically 3–10× smaller)
or `--mp4`. If it must be a GIF, combine `-q low` + `-O3 --lossy=60` +
`-m 5M`, and trim hard (`-d`).
</details>

<details>
<summary><b>Why do <code>--reverse</code> and <code>--boomerang</code> warn about memory?</b></summary>

FFmpeg's `reverse` filter (which both use) buffers every decoded frame in RAM. A 10-second
1080p clip at 30 fps is roughly a gigabyte. Lower `-r`/`-w`, or shorten the
clip with `-d`.
</details>

<details>
<summary><b>Why doesn't <code>--match-fps</code> give exact timing for GIF?</b></summary>

GIF stores each frame's delay in whole hundredths of a second, and the FFmpeg
GIF writer rounds them down. A 30 fps source therefore plays at about 33.3 fps,
and 23.976 fps plays at 25 fps. Sources above 50 fps are worse: browsers slow
frames shorter than 20 ms down. The tool warns when the difference is over
5%. For exact timing use `--webp`, `--apng` or `--mp4`.
</details>

<details>
<summary><b>Does <code>--audio</code> respect speed/reverse/fade?</b></summary>

No — the soundtrack is muxed as-is (with `-shortest`). Those effects are
video-only by design.
</details>

<details>
<summary><b>Windows support?</b></summary>

Yes — everything works if `ffmpeg`/`ffprobe` are on `PATH`; ANSI colors and
`os.startfile` are handled automatically. Clipboard support depends on your
terminal environment.
</details>

<details>
<summary><b>Can I see what it would run without running it?</b></summary>

`--dry-run` prints the exact FFmpeg/gifsicle commands to stdout and writes no
output files. Local input files are still probed with ffprobe (read-only). For
stdin (`-`) and URL inputs it neither reads the pipe nor downloads anything: it
plans with a placeholder 1920×1080 source, skips `--auto-crop`, and `--fade`
needs an explicit `--duration`. The printed commands reference temporary
paths, so treat them as a preview rather than copy-paste scripts.
</details>

<details>
<summary><b>How do I make conversion faster?</b></summary>

Smaller `-w` and `-r` dominate. For batches, use `-j` plus `--threads` to
balance across cores, and `--hwaccel auto` to offload decoding.
</details>

## 🤝 Contributing

Issues and PRs are welcome! Ideas that would fit nicely:

- [ ] Reversed seamless loops (`--fade` on the reversed timeline)
- [ ] `--colors` as a `--max-size` search lever
- [ ] Subtitle-file captions (`--text-file`)
- [ ] A small smoke-test suite against a generated fixture clip

Before submitting a feature PR, please check it against `--dry-run` output
and include an example command in the description.

## 📄 License

MIT — see [LICENSE](LICENSE).

<div align="center">
<sub>If this tool saved you a Premiere round-trip, consider leaving a ⭐</sub>
</div>
