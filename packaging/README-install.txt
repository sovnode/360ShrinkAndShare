360 Shrink and Share 1.3 - Read me
==================================
(called 360 Video Shrinker until version 1.2)

Turns big 360-degree videos into small files that keep most of what they're good for: you can still
look all around every spot of a walk-through. It keeps ONE SHARP PANORAMA EVERY FEW SECONDS and writes
either
- a 360 slideshow (.mp4): each panorama shown 1.5 s, every one a chapter named by its time in the
  original, tagged as 360 so VLC opens it in its look-around view; or
- a folder of 360 JPEG panoramas, each tagged as a 360 photo; or
- a 12 fps video: the whole video at 12 frames a second with its sound, a chapter on the sharpest
  frame of every 2 s (pause, then Shift+N / Shift+P in VLC).
New in 1.3: any of these can be made as ONE BROWSER PAGE (.html) instead. It opens in Edge or Chrome
by double-click and has its own 360 viewer inside: nothing to install, no VLC, no internet. Made for
sending 360 material to people who can't install anything. See BROWSER PAGES below.

Works with equirectangular 360 videos (as exported by Insta360 Studio and similar) up to 8K, 8- and
10-bit, H.264 or HEVC. Typical result with Slideshow movie full quality: 1/60 to 1/170 of the original's size.
The GPU does the heavy work where the PC has one (Intel Quick Sync, NVIDIA NVENC), otherwise the CPU;
a 4-minute 5.7K survey takes about 15 seconds with a recent GPU.


START
- Window: Start menu or Desktop > 360 Shrink and Share. Add videos or a folder, pick a preset, press
  Start. The Status column says where each one was made ("Done - Intel GPU").
- File Explorer: right-click one or more 360 videos (.mp4 .mov .m4v) > Show more options (or
  Shift + right-click) > 360 Shrink and Share > a preset. The window opens and converts them.
  "Open in 360 Shrink and Share..." lists them without starting. On a folder (or inside one):
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


BROWSER PAGES (one .html file with the pictures and the viewer inside)
- Make one: tick "Browser page (.html)" in the window before Start (it applies to whichever preset you
  picked), or use the preset "360 photo album for a browser", or tick "Browser page" in a preset of
  your own so the right-click menu makes pages too.
- From files you already have: File > Browser page from a 360 video or slideshow (.mp4)... and
  File > Browser album from a folder of 360 photos... (for example interval-timer photos from the
  camera; photos wider than 8192 px are reduced to that).
- In the page: drag to look around, mouse wheel or + / - to zoom, F for full screen.
    Photo album: Left / Right arrow = previous / next photo, Home / End, T = thumbnails. The direction
    you look in is kept from photo to photo.
    Video: Space = play / pause, Left / Right arrow = previous / next panorama (slideshow) or sharp
    frame (12 fps video), click or drag the bar to jump, M = sound.
- Size: a page is 1/3 bigger than the video or photos in it. A photo album has no fixed limit (100
  photos at 5760 px are about 200 MB). A video can be up to 1.5 GB (tested in Edge and Chrome); a
  bigger one is kept as the .mp4 and the result says so.
- HEVC video in a page needs a PC whose graphics chip plays HEVC (most since about 2017); where it
  doesn't, the page says so. The photo album and the H.264 preset work everywhere.
- Slideshows made by version 1.0 can't be played by browsers: convert the original again.
- Why one file: a browser doesn't let a page opened from disk put a separate video or photo file
  into a 360 view, so everything is inside the page.


PRESETS (every one picks the sharpest frame of its interval; slideshows are silent, 1.5 s per panorama)
  Reduce frame rate to 12fps    - the whole video at 12 fps with its sound, every frame at the same
                                  quality (qp 26, NVIDIA GPU), a chapter on the sharpest frame of every 2 s
  Slideshow movie full quality  - 1 panorama / 5 s at full resolution, HEVC   about 1/60 (5.7K) to 1/170 (4K)
  Slideshow movie low quality   - 1 / 5 s at 2880 px, HEVC, skips repeated views
  Slideshow high compatibility  - 1 / 5 s at full resolution, H.264, for PCs without HEVC
  Converter to 360 photos       - folder of full-resolution JPEG panoramas, one every 5 s
  360 photo album for a browser - one .html page: a 360 photo every 5 s (up to 5760 px), arrow keys
Presets > Edit presets... makes new ones or changes these (interval, width, codec, quality, GPU,
which ones show in the right-click menu). "Restore built-in presets" brings back any you deleted.
HEVC plays in VLC everywhere; if another player can't play a slideshow, use Slideshow high compatibility (H.264).


WHERE FILES GO, AND WHAT IS NEVER DONE
- Outputs go next to the original, named like
  "<video> (360 slideshow 5s 5760px HEVC, 1.5s each).mp4", with the original's date. Browser pages
  have the same names ending in .html (an album: "<video> (360 album 5s 5760px).html").
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
%LOCALAPPDATA%\360 Shrink and Share holds presets.json, settings.json, log\conversions.jsonl (every
conversion) and work\ (outputs are built here, then moved into place). Tools > Open the data folder.


COMMAND LINE (optional, for scripts)
  "%LOCALAPPDATA%\Programs\360 Shrink and Share\python\python.exe" ^
  "%LOCALAPPDATA%\Programs\360 Shrink and Share\app\shrink360.py" <command>
    presets                          list presets
    probe FILE... [--preset NAME]    what a file is; estimated output size
    scan FOLDER... [--preset NAME] [--report out.csv]
    convert FILE... --preset NAME [--html] [--out DIR] [--set key=value ...]
    html PATH... [--width N]         a .mp4 or a folder of 360 photos -> one browser page
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
