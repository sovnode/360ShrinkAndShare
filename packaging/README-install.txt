360 Video Shrinker 1.1 - Read me
================================

Turns big 360-degree videos into small files that keep most of what they're good for: you can still
look all around every spot of a walk-through. It keeps ONE SHARP PANORAMA EVERY FEW SECONDS and writes
either
- a 360 slideshow (.mp4): each panorama shown 1.5 s, every one a chapter named by its time in the
  original, tagged as 360 so VLC opens it in its look-around view; or
- a folder of 360 JPEG panoramas, each tagged as a 360 photo; or
- a 12 fps video: the whole video at 12 frames a second with its sound, a chapter on the sharpest
  frame of every 2 s (pause, then Shift+N / Shift+P in VLC).

Works with equirectangular 360 videos (as exported by Insta360 Studio and similar) up to 8K, 8- and
10-bit, H.264 or HEVC. Typical result with Slideshow movie full quality: 1/60 to 1/170 of the original's size.
The GPU does the heavy work where the PC has one (Intel Quick Sync, NVIDIA NVENC), otherwise the CPU;
a 4-minute 5.7K survey takes about 15 seconds with a recent GPU.


START
- Window: Start menu or Desktop > 360 Video Shrinker. Add videos or a folder, pick a preset, press
  Start. The Status column says where each one was made ("Done - Intel GPU").
- File Explorer: right-click one or more 360 videos (.mp4 .mov .m4v) > Show more options (or
  Shift + right-click) > 360 Video Shrinker > a preset. The window opens and converts them.
  "Open in 360 Video Shrinker..." lists them without starting. On a folder (or inside one):
  "Find 360 videos in this folder..." lists every 360 video in it and its subfolders.
- Tools > Right-click menu in File Explorer turns that menu on or off.


WATCHING THE RESULTS IN VLC (free, videolan.org)
- Drag with the mouse to look around; the mouse wheel zooms.
- Each panorama is shown 1.5 s; Space pauses on the one you want.
- Shift+N / Shift+P: next / previous panorama, paused or playing. Playback > Chapter lists them by
  their time in the original video.
- E (next frame) also steps one panorama; pressed very fast VLC 3 stops after about 18 - press Space
  twice to get it going again.
JPEG panoramas open in 360 photo viewers (they carry the 360-photo tag); any image viewer shows them
as flat 2:1 pictures.


PRESETS (every one picks the sharpest frame of its interval; slideshows are silent, 1.5 s per panorama)
  Reduce frame rate to 12fps    - the whole video at 12 fps with its sound, every frame at the same
                                  quality (qp 26, NVIDIA GPU), a chapter on the sharpest frame of every 2 s
  Slideshow movie full quality  - 1 panorama / 5 s at full resolution, HEVC   about 1/60 (5.7K) to 1/170 (4K)
  Slideshow movie low quality   - 1 / 5 s at 2880 px, HEVC, skips repeated views
  Slideshow high compatibility  - 1 / 5 s at full resolution, H.264, for PCs without HEVC
  Converter to 360 photos       - folder of full-resolution JPEG panoramas, one every 5 s
Presets > Edit presets... makes new ones or changes these (interval, width, codec, quality, GPU,
which ones show in the right-click menu). "Restore built-in presets" brings back any you deleted.
HEVC plays in VLC everywhere; if another player can't play a slideshow, use Slideshow high compatibility (H.264).


WHERE FILES GO, AND WHAT IS NEVER DONE
- Outputs go next to the original, named like
  "<video> (360 slideshow 5s 5760px HEVC, 1.5s each).mp4", with the original's date.
- Nothing is ever overwritten: if the output exists, the video is skipped.
- Every output is checked before it is put in place (every frame decodes, count, length, 360 tags,
  chapters).
- Originals are never changed or deleted. Only on request: right-click finished rows > Send the
  original(s) to the Recycle Bin... (asks first; only for videos with a checked conversion). OneDrive
  files deleted this way leave OneDrive too (its recycle bin keeps them for a while).
- OneDrive online-only files are not opened while looking (that would download them). Converting
  one asks first and downloads it.
- Camera originals (.insv, .360, .lrv) and unstitched dual-fisheye videos can't be converted: export
  them as 360 MP4 from Insta360 Studio first.


YOUR DATA
%LOCALAPPDATA%\360 Video Shrinker holds presets.json, settings.json, log\conversions.jsonl (every
conversion) and work\ (outputs are built here, then moved into place). Tools > Open the data folder.


COMMAND LINE (optional, for scripts)
  "%LOCALAPPDATA%\Programs\360 Video Shrinker\python\python.exe" ^
  "%LOCALAPPDATA%\Programs\360 Video Shrinker\app\shrink360.py" <command>
    presets                          list presets
    probe FILE... [--preset NAME]    what a file is; estimated output size
    scan FOLDER... [--preset NAME] [--report out.csv]
    convert FILE... --preset NAME [--out DIR] [--set key=value ...]
    gpu                              what the GPUs here can encode and decode
    log [--last N]                   what was done
    recycle FILE... [--yes]          dry run, or send checked originals to the Recycle Bin
    menu install|remove|status       Explorer right-click menu
  selftest.py in the same folder checks the whole program on synthetic clips (a few minutes).


TROUBLESHOOTING
- Nothing in the right-click menu: Windows 11 shows it under "Show more options"; or turn it on in
  Tools > Right-click menu in File Explorer.
- "Done - CPU" and slow: the PC has no usable GPU encoder; Tools > Check the GPU... shows what it has.
- Set the environment variable V360_NO_GPU=1 to switch the GPU off.
