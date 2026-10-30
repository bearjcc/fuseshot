# Fuseshot quick start

Fuseshot looks through a photo library for shots taken a few seconds apart
and turns them into panoramas, best-face group photos, looping action shots
and (with extra programs) 3D splats. It only reads your photos. Results go to
a separate folder, and only the ones you keep.

Windows 10/11 and Linux. Python 3.11, 3.12 or 3.13.

## 1. Install

### Windows

1. Install Python from https://www.python.org/downloads/windows/ and tick
   "Add python.exe to PATH" in the installer.
2. Install Hugin from https://hugin.sourceforge.io/download/ (the 64-bit
   Windows installer in the files section). Keep the default folder,
   `C:\Program Files\Hugin`; Fuseshot finds it there on its own.
3. Open PowerShell in the folder holding the `fuseshot-*.whl` file and run:

       py -m venv $HOME\fuseshot-venv
       & $HOME\fuseshot-venv\Scripts\Activate.ps1
       pip install "fuseshot-0.1.0-py3-none-any.whl[group]"
       fuseshot doctor

   If PowerShell refuses to run `Activate.ps1`, run
   `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once and try again.
   In a new window, activate the venv again (the second line) before using
   `fuseshot`.

### Linux (Debian, Ubuntu, Mint)

    sudo apt install python3-venv hugin-tools enblend enfuse
    python3 -m venv ~/fuseshot-venv
    . ~/fuseshot-venv/bin/activate
    pip install "fuseshot-0.1.0-py3-none-any.whl[group]"
    fuseshot doctor

Other distributions: the package holding `nona`, `cpfind` and `enblend` is
usually called `hugin` or `hugin-tools`.

`fuseshot doctor` lists what it found: Hugin, the graphics card, memory, and
which optional parts are there. Anything marked missing comes with the
command that fixes it. Until step 2 is done it also says no photo folders
are set up yet.

`[group]` adds best-face group photos (MediaPipe, about 60 MB). Leave it out
if you don't want them.

## 2. Point it at your photos

    fuseshot init

It asks for the folders holding your photos (type a path, or press Enter to
pick one in a window) and where kept results should go (Pictures\Fuseshot
by default). Every folder below each one is read. You can give the folders
straight away too:

    fuseshot init "D:\Photos" "E:\Takeout" --output-folder "D:\Fuseshot results"

On Linux the same with Linux paths, for example
`fuseshot init ~/Pictures/Camera ~/Takeout --output-folder ~/Fuseshot-results`.

Without init, name the folder on each run. Note `--recursive`: without it
only the folder itself is read, not the folders inside it.

    fuseshot --source folder:"D:\Photos" --recursive run

## 3. Try one year first

A big library takes hours the first time (see "How long it takes"). Start
with one year to see what it finds:

    fuseshot run --year 2019

`--since 2019-06-01 --until 2019-08-31` works too. A later full `fuseshot run`
reuses everything the first one did.

## 4. Run it and look at the results

    fuseshot run
    fuseshot review --open

`run` reads the photo details, finds the sets, checks which really overlap,
stitches previews, and looks for action shots and group photos. Each stage
shows how far it has got and roughly how long is left.

The review page opens in the browser. For each result: **Keep** copies it
to the results folder, **Discard** hides it, **Edit** tries another
projection, crop or stitching model. Nothing is ever written next to the
original photos.

The review page shows 3000 pixel copies so it stays quick. A kept panorama
or group photo is made again at the full size of the photos, from the same
stitch you looked at, and that is what lands in the results folder. Big
panoramas take a minute or two to finish after Keep; the bar at the top of
the review page and the log show how far the full-size render has got. Each kept file gets
the date, camera make and model, and GPS position (where the photos have
one) of the first photo in the set, so photo apps sort it in with the rest. An AI filled
version stays at the review size, because that is the size the fill is made
at.

Full size is limited only by memory and by the largest picture a JPEG can
hold. To keep files smaller, set a cap in megapixels:

```
fuseshot --max-megapixels 100 export
```

or put `max_megapixels = 100` in `fuseshot.toml` to make it stick.

A kept panorama is cut to the largest rectangle with no empty corners, the
way most cameras save one. To keep the whole uncropped outline for one
panorama, open it, untick **Crop** (or untick it under **Edit**), then Keep.
To keep every panorama uncropped, put `crop = false` in `fuseshot.toml`.
AI fill, which paints in the corners instead, is never used unless you tick
it for a panorama.

Stopping is safe. Press Ctrl+C (or switch the computer off) at any point and
run the same command again: it carries on where it stopped and does not redo
finished work. Running it again on an unchanged library takes seconds, so it
is fine to run after copying new photos in.

If a run finds nothing, it says why (photos in subfolders without
`--recursive`, photos with no camera time, too few photos, nothing
overlapping) and what to try.

## Google Takeout

Fuseshot reads a Google Photos export directly, including the `.json` files
Google puts next to each photo (time taken, place, camera).

1. At https://takeout.google.com, choose "Deselect all", then tick only
   **Google Photos**.
2. Choose .zip and the 50 GB file size, so there are as few parts as
   possible. A big library still comes in several zips.
3. Extract **all** the zips under one folder, for example `D:\Takeout`.
   Either merge them into one `Takeout\Google Photos` tree, or let each zip
   unpack into its own folder (`takeout-...-001`, `takeout-...-002`) side by
   side. Google often puts a photo in one zip and its `.json` in another;
   both layouts find it. A part unpacked after a run is picked up by the
   next one.
4. Point Fuseshot at it: `fuseshot init "D:\Takeout"`.

What it handles: the shortened and `supplemental-metadata` names of the
`.json` files, `(1)` duplicates, `-edited` copies (also `-bearbeitet`,
`-modifié` and the other languages Google uses; the original is used when
both are there), the same photo in a year folder and in albums (counted once), and
motion photos (the still picture is used; the video part is ignored). When a
photo's own EXIF time looks right, it wins over the `.json` time.

## What it reads

| Kind | Status |
| --- | --- |
| JPEG, PNG, TIFF, WebP | read |
| HEIC/HEIF (iPhone, recent Pixel and Samsung) | read |
| JPEG XL | with `pip install pillow-jxl-plugin` |
| Camera raw: Sony ARW/SRF/SR2, Google Pixel and other DNG, Canon CR2/CR3, Nikon NEF/NRW, Olympus ORF, Panasonic RW2, Fujifilm RAF, Pentax PEF | read through LibRaw (rawpy). Tested on a Sony A7 III ARW and a Pixel 8 Pro DNG; the others use the same library but were not tried |
| RAW + JPEG pairs | treated as one shot; the JPEG is used. Tested with a real Sony ARW and the preview JPEG taken out of it |
| Pixel burst folders and motion photos (`.MP.jpg`) | grouped as bursts; the still is used. Tested on made-up files with real Pixel names, not on a real burst |
| Videos | ignored |
| Scanned prints and film | not supported and not tested. Scans have no camera time, so they are never grouped; film has no bursts |

Sets are found from the time and camera stored in each photo. Pictures
saved from chat apps and most screenshots carry neither, so they are left
out. The time comes from the EXIF time taken, else the Takeout `.json`, else
a time in the file name (`IMG_20240108_080218.jpg`), else the EXIF time the
file was last saved, else the file's own date.

Very large pictures (up to a gigapixel, such as a phone's own panoramas) are
read too.

## The graphics card (NVIDIA)

Nothing to set up for the basics: Fuseshot finds an NVIDIA card through the
driver (`nvidia-smi`) and uses it where it can, and uses the processor when
there is no card or something goes wrong. `fuseshot doctor` shows what it
found. Panoramas, group photos and action loops run on the processor either
way; the card only speeds up the two optional extras below.

Keep the NVIDIA driver reasonably current (version 560 or newer, from
https://www.nvidia.com/Download/index.aspx or the distribution's driver
manager). Turing cards (RTX 20xx) and newer work with everything here.

**AI fill** (fills the empty corners of a panorama instead of cropping
them). Install PyTorch built for CUDA into the same venv. The plain
`pip install torch` gets a processor-only build on Windows, so use the CUDA
index:

    pip install torch --index-url https://download.pytorch.org/whl/cu126

This is a 2.5 GB download. The fill model takes about 1.5 GB of graphics
memory at its peak (measured), so an 8 GB card has plenty of room. Without
CUDA it still works, at a few minutes per panorama.

**3D splats** (experimental, for objects or rooms photographed while walking
around them). They need two programs:

- COLMAP 4.x. On Windows, `colmap-x64-windows-cuda.zip` from
  https://github.com/colmap/colmap/releases, unpacked to
  `C:\Program Files\COLMAP`. On Linux, distribution packages are usually old
  and built without CUDA; `conda install -c conda-forge colmap` gets a recent
  CUDA build. Fuseshot uses the card when COLMAP was built with CUDA and
  falls back to the processor if that fails.
- Brush, from https://github.com/ArthurBrussee/brush/releases
  (`brush-app-x86_64-pc-windows-msvc.zip` or the Linux `.tar.xz`), unpacked
  into the `tools` folder inside Fuseshot's home folder, keeping the
  `brush-app...` folder name. `fuseshot doctor` shows where that is. Brush draws through Vulkan or DirectX 12, which every current NVIDIA
  driver includes.

Hugin can also remap on the graphics card (`nona -g`), but it is unreliable
with current drivers and gains little next to blending, so Fuseshot doesn't
use it.

## How hard it works the computer

Fuseshot sizes itself from the computer it runs on: threads from the number
of processor cores, and memory from what is free when it starts (at most
half of the total, always leaving room for other programs). A small laptop
gets one or two threads; a big desktop gets many more. It never holds the
whole library in memory, so a library of hundreds of thousands of photos
needs no more memory than a small one.

To use less (to keep working while it runs) or more:

    fuseshot --workers 4 --memory-mb 8000 run

`FUSESHOT_WORKERS` and `FUSESHOT_MEMORY_MB` do the same for every run.

## How long it takes

Test library: 200,203 made-up photos (small JPEGs, a Takeout-style folder, Sony
and Pixel raw names, 40 real panorama sets hidden among 10,779 look-alike
bursts) on an SSD, 8 cores, 16 GB of memory.

| Step | Time | Peak memory |
| --- | --- | --- |
| First pass, no stitching | 2.5 min | 490 MB |
| Stitching the 40 panoramas | about 5 s each | 450 MB |
| Second run, nothing changed | 14 s | 290 MB |

The test files are tiny, so these times are for the bookkeeping, not for
reading real photos. On a real library expect the first pass to be limited by
the disk: every photo's header is read once and every candidate set gets small
previews made from the full file. As a rough guide, 200,000 real photos on a
fast SSD take somewhere around 30 to 60 minutes for the first pass, and several
hours from a USB hard drive. Raw files are slower to preview than JPEGs.
Stitching a real panorama takes about 1 to 5 minutes, depending on size and
frame count. Memory stayed under 500 MB at 200,000 photos and is checked
against what the machine has free (see "How hard it works the computer").

Stopping is safe at any point (Ctrl+C, closing the window, a crash or power
cut). The next run carries on: files already read, sets already measured and
panoramas already stitched are not done again. Only the one panorama that was
being stitched when it stopped starts over.

## When something goes wrong

- A damaged or cut-short photo is named in the output and skipped; the rest
  of the run carries on. So is a folder Fuseshot is not allowed to open.
- A full disk stops the run with a message saying which disk. Free some space
  and run the same command again: nothing finished is lost, and a panorama
  that ran out of room is tried again.
- Windows paths longer than 260 characters need long paths turned on, once,
  from an administrator PowerShell:

      New-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem -Name LongPathsEnabled -Value 1 -PropertyType DWord -Force

  That covers Fuseshot reading your photos. Hugin is another matter: it will
  not run in a folder much past 200 characters even with long paths on, so
  keep the home folder short (`--home D:\fs`). `fuseshot doctor` warns when
  it is too long.
- `-v` before the command (`fuseshot -v run`) prints more detail. The outside
  programs (Hugin and the rest) log to `work\<set>\stitch.log` and
  `render.log` in the home folder, and their errors name the file.

## Updating and removing

Install a newer wheel over the old one, in the same venv (use the new file's name):

    pip install --upgrade fuseshot-0.2.0-py3-none-any.whl

If pip says the version is already installed (a rebuilt wheel with the same
number), force it:

    pip install --force-reinstall --no-deps fuseshot-0.1.0-py3-none-any.whl

Your decisions, the photo index and the results are in the home folder, not
in the venv, so they survive updates. Run `fuseshot run` after updating;
it picks up where the old version stopped.

To remove it: `pip uninstall fuseshot`, or delete the venv folder. That
leaves the home folder and the results folder alone; delete those yourself
if you want them gone (`fuseshot doctor` prints the home folder's path).

## Where things are

- Kept results: the output folder you chose (default `Pictures\Fuseshot`).
- Everything else (the photo index, previews, work folders, settings file
  `fuseshot.toml`): Fuseshot's home folder, `%LOCALAPPDATA%\fuseshot` on
  Windows and `~/.local/share/fuseshot` on Linux. It can grow to a few GB for
  a big library. Deleting it loses nothing you kept, only the work done so far.

## Known limits

- The detector was tuned on one family's photos from a handful of phones and
  cameras. Expect some missed sets and some odd guesses; the review page is
  where you sort them out.
- Walk-along panoramas stitch less reliably than turning on the spot.
- AI-filled corners look soft next to the real photo.
- Splats are slow and only some sets work.
- Scans and film are not supported (see the table above).
