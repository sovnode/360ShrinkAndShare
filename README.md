# 360 Video Shrinker

Turns big 360-degree videos (walk-throughs) into small files that keep most of what
they're good for: you can still look all around every spot. It keeps **one sharp panorama every few
seconds** and writes either

- a **360 slideshow** (`.mp4`): each panorama shown 1.5 s, every one a keyframe and a chapter named by
  its time in the original, tagged as 360 so VLC opens it in its look-around view; or
- a **folder of 360 JPEG panoramas**, each tagged as a 360 photo (GPano); or
- a **12 fps video** (new in 1.2): the whole video stays playable, at 12 frames a second with its
  sound and every frame at the same quality, and the sharpest frame of every 2 s (any frame, not only the
  original's keyframes) is a keyframe with a chapter on it. Play it as a video, or pause and press
  Shift+N / Shift+P to jump from sharp frame to sharp frame. About 1/3 of an 8K export at qp 22
  (as sharp as the original), about 1/6 at qp 26 (the default).

Works with equirectangular 360 videos up to 8K (7680 x 3840), 8- and 10-bit, H.264 or HEVC, mono or
stereo. Typical result: **1/20 to 1/600 of the original's size** (1/60 to 1/170 with the full-quality
slideshow preset). The GPU does the heavy work where there is one, so a 4-minute 5.7K survey takes about 15
seconds with a recent GPU.

Windows 10 / 11, 64-bit.

## Install

**Easiest:** download `360 Video Shrinker <version>.zip` from the
[Releases](../../releases) page. It includes its own Python and video libraries; no admin rights.

1. Right-click the zip > Properties > tick **Unblock** (if shown) > OK.
2. Right-click > **Extract All...**, open the folder, double-click **Install.cmd**.
3. It installs to `%LOCALAPPDATA%\Programs\360 Video Shrinker` and adds Start-menu and Desktop shortcuts,
   a File Explorer right-click menu and an entry in Settings > Apps (which uninstalls it).

**From source:** Python 3.14 (64-bit, with tkinter) and `av` (PyAV), `numpy` and `Pillow`:

```
pip install av numpy pillow
pythonw app.pyw                  the window
python shrink360.py --help       the command line
python selftest.py               checks everything on synthetic clips (a few minutes)
```

## Use

- **Window:** add videos or a folder, pick a preset, press **Start**. The Status column says where each
  one was made ("Done - Intel GPU").
- **File Explorer:** right-click 360 videos (.mp4 .mov .m4v) > **360 Video Shrinker** > a preset
  (Windows 11: under **Show more options**). On a folder: **Find 360 videos in this folder...**.
  Turn the menu on or off in **Tools > Right-click menu in File Explorer** (per user, HKCU).

### Watching the results in VLC

- Drag to look around; the mouse wheel zooms. Space pauses.
- **Shift+N / Shift+P**: next / previous panorama (they are chapters). **Playback > Chapter** lists them
  by their time in the original video.
- **E** (next frame) also steps one panorama at a time.
- 12 fps videos: play normally, or pause and use **Shift+N / Shift+P**: each chapter is the sharpest
  frame of its 2 s. Every frame has the same quality.

## Presets

| Preset | What it makes |
|---|---|
| Reduce frame rate to 12fps | the whole video at 12 fps with its sound, every frame at qp 26 (NVIDIA GPU), a chapter on the sharpest frame of every 2 s |
| Slideshow movie full quality | 1 panorama / 5 s at the video's own resolution, HEVC crf 30 (measured: 1/59 of a 5.7K video, 1/174 of a 4K one) |
| Slideshow movie low quality | 1 / 5 s at 2880 px, HEVC crf 32, skips repeats |
| Slideshow high compatibility | 1 / 5 s at the video's own resolution, H.264 crf 24, for PCs without HEVC |
| Converter to 360 photos | folder of JPEG panoramas at the video's own resolution, quality 88, 1 / 5 s |

Every preset picks the sharpest frame of its interval.

Edit them in the window (**Edit presets...**): interval, width, sharpest or nearest frame, codec (HEVC,
H.264, AV1), quality, GPU, timing (1.5 s per panorama or the original's clock, with sound), skipping
repeats while the camera stands still, and which presets appear in the right-click menu.

A 12 fps video has one quality setting of its own, **12 fps video qp**, used for every frame (26 by
default; 22 measured as sharp as the original and about 70 % bigger). There is no separate quality for the
frames between chapters: with softer frames between, the whole video looks soft while it plays. Its
interval is at most 5 s, because each interval's frames are held in memory until the sharpest is known
(about 1 GB per 2 s at 8K).

How the sharp frame is chosen: each candidate is shrunk to a 640 px grey copy and scored by the variance
of its Laplacian over the middle 60 % of the height (the stretched zenith and nadir are left out), so the
steadiest frame of each interval wins.

## The GPU

| Job | Done on |
|---|---|
| HEVC slideshow | Intel GPU (Quick Sync), else NVIDIA GPU (NVENC), else CPU (x265) |
| H.264 slideshow up to 4096 px wide | Intel GPU, else NVIDIA GPU, else CPU (x264) |
| H.264 wider than 4096 px, AV1 | CPU (x264, SVT-AV1) |
| 12 fps video (HEVC) | the preset's GPU (built-in preset: NVIDIA; Automatic = Intel, then NVIDIA), else CPU (x265) |
| Reading HEVC | NVIDIA GPU (NVDEC), else any GPU through D3D11, else CPU |

The preset's crf is turned into the GPU's constant quantizer with a table calibrated on real survey frames,
so a GPU file has about the same picture quality (PSNR) as the CPU encoder's. If a GPU run fails or its
file doesn't pass the check, the video is redone on the next GPU, then the CPU. `V360_NO_GPU=1` turns the
GPU off. **Tools > Check the GPU...** (or `shrink360.py gpu`) shows what the GPUs in your PC can do.

## Safety

- Outputs go next to the original, named `<video> (360 slideshow 5s 5760px HEVC, 1.5s each).mp4` or
  `<video> (360 frames 5s 3840px)\0012s 360.jpg` or `<video> (360 chapters 2s 7680px 12fps).mp4`.
  Nothing is ever overwritten.
- Every output is checked before it is moved into place: every frame decodes, count and length match,
  the 360 tags, keyframes and chapters are there.
- **Originals are never changed or deleted**, except on request (right-click finished rows > **Send the
  original(s) to the Recycle Bin...**, or `shrink360.py recycle --yes`), and only when the log holds a
  checked conversion of exactly that file.
- OneDrive online-only files are not opened while scanning (that would download them); converting one
  asks first.
- Camera originals (`.insv`, `.360`) and unstitched dual-fisheye videos are listed but not converted:
  export them as 360 MP4 from the camera's software first.

## Command line

```
python shrink360.py <command>
  presets                                   list presets
  probe FILE... [--preset NAME]             what a file is; estimated output size
  scan FOLDER... [--preset NAME] [--report out.csv] [--min-mb 20]
  convert FILE... --preset NAME [--out DIR] [--set key=value ...] [--allow-download]
  gpu                                       what the GPUs here can encode and decode
  log [--last N]                            what was done
  recycle FILE... [--yes]                   dry run, or send checked originals to the Recycle Bin
  menu install|remove|status                File Explorer right-click menu
```

`FILE` may be `@list.txt` (one path per line); `--json` gives machine-readable output.

## Data folder

Presets, settings, the conversion log and the work folder are in `%LOCALAPPDATA%\360 Video Shrinker`.
`V360_DATA_DIR` overrides it, and so does `data_dir` in an `install.json` beside `paths.py` (the
installer writes one; a source checkout can have its own, which is not committed).

## Building the install package

```
powershell -ExecutionPolicy Bypass -File package.ps1
```

makes `dist\360 Video Shrinker <version>.zip` from a local Python 3.14 (python.org) with numpy, Pillow and
PyAV. It contains no presets, settings, logs or other personal files; the build stops if one turns up.

## Files

| File | Purpose |
|---|---|
| `app.pyw` | starts the window; right-click calls become job files in the data folder (one window at a time) |
| `gui.py` | the window and the preset editor (tkinter) |
| `engine.py` | probe, frame picking, GPU / CPU encoding with fallback, chapters, JPEGs, checks, log, Recycle Bin, scan |
| `mp4meta.py` | reads and writes the 360 tags (Google Spherical V1 + V2) |
| `presets.py` | preset fields, built-in presets, load / save / validate |
| `contextmenu.py` | the File Explorer right-click menu (HKCU) |
| `shrink360.py` | command line |
| `selftest.py` | builds synthetic clips (8K, 10-bit, untagged, fisheye, 16:9, stereo) and checks everything |
| `make_icon.py` | draws `icon.ico` |
| `packaging\` | the per-user installer (`setup.py`, `Install.cmd`) and its read-me files |

## Licence

The program is released under the MIT licence (see `LICENSE`). The release zip also contains Python
(PSF licence), numpy (BSD), Pillow (MIT-CMU) and PyAV with FFmpeg (BSD / LGPL / GPL, built with x264 and
x265); their licence files are in the package's `python` folder.
