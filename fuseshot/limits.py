"""How hard to work this computer, sized from what it has when Fuseshot starts:
its cores, its memory and how much of that is free. Small computers get safe
floors; big ones are used more, but the computer always stays usable.

--workers and --memory-mb (or FUSESHOT_WORKERS and FUSESHOT_MEMORY_MB) override
the guesses for one run. Kept results are made at the photos' own resolution as
far as the memory budget allows; --max-megapixels (FUSESHOT_MAX_MEGAPIXELS, or
max_megapixels in the settings file) sets a lower cap."""

import os

WORKERS = None  # set from --workers
MEMORY_MB = None  # set from --memory-mb
MAX_MEGAPIXELS = None  # set from --max-megapixels
FULL_BYTES_PER_PX = 32  # what making a full size result holds per output pixel, all copies counted
JPEG_MAX_SIDE = 65500  # the longest side a JPEG can have
PREVIEW_MB = 250  # about what one thread reading and matching previews holds
CPFIND_MB = 1500  # about what one cpfind thread takes on a big set
TOOL_FLOOR_MB = 1500  # what a small set needs to stitch at all


def _override(value, env):
    if value:
        return int(value)
    text = os.environ.get(env, "").strip()
    return int(text) if text.isdigit() and int(text) > 0 else None


def cores():
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 2


def _memory():
    try:
        import psutil

        vm = psutil.virtual_memory()
        return vm.total // 1048576, vm.available // 1048576
    except Exception:
        return 8192, 4096


def memory_mb():
    return _memory()[0]


def budget_mb():
    """Memory Fuseshot lets itself use: --memory-mb if given, else half the memory
    or what is free now less a gigabyte for everything else, whichever is smaller."""
    given = _override(MEMORY_MB, "FUSESHOT_MEMORY_MB")
    if given:
        return given
    total, free = _memory()
    return max(1024, min(total // 2, free - 1024))


def max_megapixels():
    """The cap on a kept result's size in megapixels, or None for no cap."""
    given = _override(MAX_MEGAPIXELS, "FUSESHOT_MAX_MEGAPIXELS")
    if given:
        return given
    from . import config

    value = config.settings().get("max_megapixels")
    return value if isinstance(value, (int, float)) and value > 0 else None


def full_scale(width, height, bytes_per_px=FULL_BYTES_PER_PX):
    """How far to scale a result of width x height down (1.0 for not at all) so it
    fits the memory budget, the megapixel cap and the JPEG size limit."""
    pixels = max(1, width * height)
    allowed = budget_mb() * 1048576 / bytes_per_px
    cap = max_megapixels()
    if cap:
        allowed = min(allowed, cap * 1e6)
    return min(1.0, (allowed / pixels) ** 0.5, JPEG_MAX_SIDE / max(1, width, height))


def workers():
    """Threads for reading and matching previews: a core is left for everything else,
    and no more threads than the memory budget holds."""
    given = _override(WORKERS, "FUSESHOT_WORKERS")
    if given:
        return given
    return max(1, min(cores() - 1, budget_mb() // PREVIEW_MB, 32))


def tool_mb():
    """The most one stitching tool may use before it is stopped."""
    return max(TOOL_FLOOR_MB, budget_mb())


def align_threads():
    """cpfind threads."""
    return max(1, min(cores() // 2, tool_mb() // CPFIND_MB, workers(), 8))


def sqlite_cache_kb():
    """SQLite page cache for the photo index: a 64th of the budget, 16 MB to 1 GB."""
    return max(16 * 1024, min(budget_mb() * 1024 // 64, 1024 * 1024))


_gpu = []


def gpu():
    """{"name", "vram_mb"} for the first NVIDIA card the driver reports, or None.
    Asked once; nothing is installed or needed for this to say no."""
    if not _gpu:
        import shutil
        import subprocess

        found = None
        smi = shutil.which("nvidia-smi")
        if smi is None and os.name == "nt":
            candidate = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")
            smi = candidate if os.path.exists(candidate) else None
        if smi:
            try:
                out = subprocess.run([smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                                     capture_output=True, text=True, timeout=10).stdout
                name, vram = out.strip().splitlines()[0].rsplit(",", 1)
                found = {"name": name.strip(), "vram_mb": int(float(vram))}
            except (OSError, ValueError, IndexError, subprocess.SubprocessError):
                found = None
        _gpu.append(found)
    return _gpu[0]


def colmap_cuda(colmap):
    """True when this COLMAP build was made with CUDA (its help line says so)."""
    import subprocess

    try:
        out = subprocess.run([str(colmap), "help"], capture_output=True, text=True, timeout=30,
                             env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    except (OSError, subprocess.SubprocessError):
        return False
    text = (out.stdout + out.stderr).lower()
    return "with cuda" in text and "without cuda" not in text
