"""Stitch a downloaded set with the Hugin command line tools."""
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from collections import defaultdict
from pathlib import Path

from . import parts, tools
from .progress import Progress
from .blend import blend_layers, layer_detail, plan_layers
from .files import write_atomic

LOW_SPACE = 256 << 20  # below this much free space a failed tool most likely ran out of room

PROJECTIONS = {
    'rectilinear': 0,
    'cylindrical': 1,
    'equirectangular': 2,
    'stereographic': 4,
    'transverse-mercator': 6,
    'panini': 12,
}


class StitchError(RuntimeError):
    pass


class OutOfSpace(StitchError):
    """A tool failed because the disk is full; worth trying again once there is room."""


WIN_TOOL_DIR_MAX = tools.WIN_TOOL_DIR_MAX


def check_tool_dir(cwd, windows=os.name == 'nt'):
    if not (windows and cwd):
        return
    n = len(os.path.abspath(str(cwd)))
    if n > WIN_TOOL_DIR_MAX:
        raise StitchError(f'the home folder is too long for Hugin on Windows: this set\'s work folder is {n} '
                          f'characters and Hugin stops working past about {WIN_TOOL_DIR_MAX}. Use a shorter home '
                          'folder, for example --home D:\\fs')


class Runner:
    """Runs tools, records how long each took and the peak memory it used."""

    def __init__(self, log_file):
        self.bin = tools.require('hugin').parent
        self.log_file = Path(log_file)
        self.steps = []
        self.max_mb = int(os.environ.get('FUSESHOT_MAX_MB') or 0)

    def __call__(self, tool, *args, cwd=None, check=True):
        exe = self.bin / (tool + ('.exe' if os.name == 'nt' else ''))
        cmd = [str(exe), *map(str, args)]
        check_tool_dir(cwd)
        t0 = time.time()
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    errors='replace')
        except OSError as err:
            raise StitchError(f'could not start {tool} in {cwd or os.getcwd()}: {err}') from err
        peak = [0]
        over = [False]
        stop = threading.Event()
        limit = self.max_mb * 1048576

        def watch():
            try:
                import psutil
                ps = psutil.Process(proc.pid)
            except Exception:
                return
            while not stop.is_set():
                try:
                    rss = ps.memory_info().rss
                    for child in ps.children(recursive=True):
                        rss += child.memory_info().rss
                    peak[0] = max(peak[0], rss)
                    if limit and rss > limit:
                        over[0] = True
                        for child in ps.children(recursive=True):
                            child.kill()
                        ps.kill()
                        return
                except Exception:
                    pass
                stop.wait(0.1)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        output, _ = proc.communicate()
        stop.set()
        watcher.join()
        seconds = time.time() - t0
        step = {'tool': tool, 'seconds': round(seconds, 1), 'peak_mb': round(peak[0] / 1048576)}
        self.steps.append(step)
        with open(self.log_file, 'a', encoding='utf-8') as log:
            log.write(f'\n$ {" ".join(cmd)}\n{output}\n[{step}]\n')
        if over[0]:
            raise StitchError(f'{tool} went over {self.max_mb} MB and was stopped')
        if check and proc.returncode != 0:
            free = shutil.disk_usage(self.log_file.parent).free
            if free < LOW_SPACE or re.search(r'no space left|write error|disk full', output or '', re.I):
                raise OutOfSpace(f'{tool} could not write its result: the disk holding {self.log_file.parent} '
                                  f'has {free // 1048576} MB free. Free some space and run again')
            raise StitchError(f'{tool} exited with {proc.returncode}; see {self.log_file}')
        return output


def read_exif_fov(path, f35_hint=None):
    """Horizontal field of view from the 35 mm equivalent focal length.

    f35_hint is used when the file has no 35 mm tag (older cameras); it comes
    from the focal length times the camera's crop factor.

    Phones quote the equivalent focal length for the long side of the full
    sensor, so the long side maps to 36 mm; the short side follows the
    actual aspect ratio (wide aspect modes crop the short side)."""
    from PIL import Image
    with Image.open(path) as img:
        width, height = img.size
        ifd = img.getexif().get_ifd(34665)
    f35 = ifd.get(41989) or f35_hint
    if not f35:
        return width, height, None, None
    half_long = 36 / (2 * float(f35))
    half = half_long if width >= height else half_long * width / height
    return width, height, float(f35), math.degrees(2 * math.atan(half))


def write_project(manifest, pto_path):
    """Initial project: one lens per (size, equivalent focal length)."""
    lenses = {}
    lines = ['p f2 w3000 h1500 v360 E0 R0 n"TIFF_m c:LZW"', 'm i0']
    for index, item in enumerate(manifest):
        width, height, f35, hfov = read_exif_fov(item['image'], item.get('f35_hint'))
        if hfov is None:
            hfov = 70.0
        key = (width, height, f35)
        image = Path(item['image']).resolve()
        if key in lenses:
            ref = lenses[key]
            lens = f'v={ref} Ra={ref} Rb={ref} Rc={ref} Rd={ref} Re={ref} a={ref} b={ref} c={ref} d={ref} e={ref} g={ref} t={ref}'
        else:
            lenses[key] = index
            lens = f'v{hfov:.4f} Ra0 Rb0 Rc0 Rd0 Re0 a0 b0 c0 d0 e0 g0 t0'
        lines.append(f'i w{width} h{height} f0 {lens} Eev0 Er1 Eb1 r0 p0 y0 TrX0 TrY0 TrZ0 Tpy0 Tpp0 j0 Va1 Vb0 Vc0 Vd0 Vx0 Vy0 Vm5 n"{image}"')
        item['f35'] = f35
        item['hfov'] = round(hfov, 2)
    Path(pto_path).write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return lenses


def absolute_paths(pto_path, base=None):
    """Point every image in a project at an absolute path.

    pano_modify writes image paths relative to its output file, which breaks once
    the project is copied elsewhere (render works on a copy in render_tmp)."""
    pto_path = Path(pto_path)
    base = Path(base) if base else pto_path.parent
    lines = pto_path.read_text(encoding='utf-8', errors='replace').splitlines()
    changed = False
    for index, line in enumerate(lines):
        found = re.search(r' n"([^"]+)"', line) if line.startswith('i ') else None
        if found and not Path(found.group(1)).is_absolute():
            image = (base / found.group(1)).resolve()
            lines[index] = line[:found.start(1)] + str(image) + line[found.end(1):]
            changed = True
    if changed:
        write_atomic(pto_path, '\n'.join(lines) + '\n')


def mean_error(checkpto_output):
    found = re.search(r'Mean error\s*:\s*([\d.]+)', checkpto_output)
    if found:
        return float(found.group(1))
    return None


def control_points(pto_path):
    pairs = defaultdict(int)
    for line in Path(pto_path).read_text(encoding='utf-8', errors='replace').splitlines():
        if not line.startswith('c '):
            continue
        n = int(re.search(r'\bn(\d+)', line).group(1))
        m = int(re.search(r'\bN(\d+)', line).group(1))
        pairs[tuple(sorted((n, m)))] += 1
    return dict(pairs)


def image_params(pto_path):
    """Yaw, pitch, roll and hfov per image from an optimised project."""
    images = []
    for line in Path(pto_path).read_text(encoding='utf-8', errors='replace').splitlines():
        if not line.startswith('i '):
            continue
        get = lambda k: re.search(rf'\s{k}(-?[\d.e+-]+)', line)
        images.append({k: float(get(k).group(1)) if get(k) else None for k in ('y', 'p', 'r', 'v')})
    for image in images:
        if image['v'] is None:
            image['v'] = None
    return images


def pano_line(pto_path):
    for line in Path(pto_path).read_text(encoding='utf-8', errors='replace').splitlines():
        if not line.startswith('p '):
            continue
        w = int(re.search(r'\sw(\d+)', line).group(1))
        h = int(re.search(r'\sh(\d+)', line).group(1))
        v = float(re.search(r'\sv([\d.]+)', line).group(1))
        s = re.search(r'\sS(\d+),(\d+),(\d+),(\d+)', line)
        crop = tuple(int(x) for x in s.groups()) if s else (0, w, 0, h)
        return {'w': w, 'h': h, 'v': v, 'crop': crop}
    raise StitchError(f'no p line in {pto_path}')


def components(count, pairs, min_points):
    parent = list(range(count))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for (a, b), n in pairs.items():
        if n >= min_points:
            parent[find(a)] = find(b)
    groups = defaultdict(list)
    for i in range(count):
        groups[find(i)].append(i)
    return sorted(groups.values(), key=len, reverse=True)


def describe_layout(manifest, pairs, params, min_points=8):
    count = len(manifest)
    strong = {p: n for p, n in pairs.items() if n >= min_points}
    neighbours = [(i, i + 1) for i in range(count - 1)]
    sequential = sum(1 for p in neighbours if p in strong)
    non_sequential = sorted(p for p in strong if p[1] - p[0] > 1)
    yaws = [p['y'] for p in params]
    pitches = [p['p'] for p in params]
    rows = []
    for i in sorted(range(count), key=lambda k: pitches[k]):
        if rows and abs(pitches[i] - sum(pitches[j] for j in rows[-1]) / len(rows[-1])) < 12:
            rows[-1].append(i)
        else:
            rows.append([i])
    return {
        'images': count,
        'pairs_with_points': len(pairs),
        'overlapping_pairs': len(strong),
        'sequential_neighbours_overlapping': f'{sequential}/{len(neighbours)}',
        'sequential_gaps': [f'{a + 1}-{b + 1}' for a, b in neighbours if (a, b) not in strong],
        'non_sequential_overlaps': [f'{a + 1}-{b + 1}:{strong[(a, b)]}' for a, b in non_sequential],
        'connected_groups': [[i + 1 for i in g] for g in components(count, pairs, min_points)],
        'yaw_span': round(max(yaws) - min(yaws), 1),
        'pitch_span': round(max(pitches) - min(pitches), 1),
        'pitch_rows': [[i + 1 for i in sorted(r)] for r in sorted(rows, key=lambda r: -sum(pitches[j] for j in r))],
        'per_image': [
            {
                'n': i + 1,
                'file': m['file'],
                'f35': m.get('f35'),
                'yaw': round(p['y'], 1),
                'pitch': round(p['p'], 1),
                'roll': round(p['r'], 1),
            }
            for i, (m, p) in enumerate(zip(manifest, params))
        ],
    }


MODELS = ('auto', 'rotation', 'mosaic')
MODEL_VARIABLES = {
    'rotation': 'y,p,r,v,b',
    'mosaic': 'y,p,r,TrX,TrY,TrZ',
}
MOSAIC_ADVANTAGE = 0.6

GOOD_ERROR = 3.0
POOR_ERROR = 10.0


def largest_group(count, pto_path, min_points=8):
    groups = components(count, control_points(pto_path), min_points)
    return max((len(g) for g in groups), default=0)


def solve(set_dir, run, model, auto_error, count):
    """Optimise the cleaned points under one camera model; returns its score."""
    clean = f'refine_clean_{model}.pto'
    out = f'optimised_{model}.pto'
    run('cpclean', '-w', '-o', clean, 'auto.pto', cwd=set_dir)
    run('pto_var', f'--opt={MODEL_VARIABLES[model]}', '-o', f'refine_{model}.pto', clean, cwd=set_dir)
    photometric = True
    try:
        run('autooptimiser', '-n', '-m', '-l', '-s', '-o', out, f'refine_{model}.pto', cwd=set_dir)
    except StitchError:
        photometric = False
        run('autooptimiser', '-n', '-l', '-s', '-o', out, f'refine_{model}.pto', cwd=set_dir)
    error = mean_error(run('checkpto', out, cwd=set_dir, check=False))
    kept = 'refined'
    if auto_error is not None and (error is None or error > auto_error):
        shutil.copyfile(set_dir / 'auto.pto', set_dir / out)
        error, kept = auto_error, 'auto'
    return {
        'error_px': error,
        'kept_solution': kept,
        'photometric': photometric,
        'largest_group': largest_group(count, set_dir / out),
    }


def choose_model(scores, prefer, px_scale=1.0):
    """Pick rotation or mosaic the way an automatic stitcher would: coverage first, then residual."""
    if len(scores) == 1:
        model = next(iter(scores))
        return model, 'only model tried'
    rot, mos = scores['rotation'], scores['mosaic']
    if mos['largest_group'] > rot['largest_group']:
        return 'mosaic', 'mosaic connects more images'
    if rot['largest_group'] > mos['largest_group']:
        return 'rotation', 'rotation connects more images'
    re, me = rot['error_px'], mos['error_px']
    if re is None or me is None:
        return ('rotation' if me is None else 'mosaic'), 'the other model failed'
    good = GOOD_ERROR * px_scale
    if prefer == 'mosaic' and me <= max(good, re):
        return 'mosaic', 'overlap check saw camera movement and mosaic fits as well'
    if re > good and me < MOSAIC_ADVANTAGE * re:
        return 'mosaic', f'mosaic residual {me:.2f} px vs rotation {re:.2f} px'
    return 'rotation', f'rotation residual {re:.2f} px (mosaic {me:.2f} px)'


def align(set_dir, celeste=False, multirow=False, ransac='hom', model='auto', plan=None, threads=2):
    """Find control points once, then solve under the requested camera model.

    model="auto" tries rotation and mosaic and keeps the better one. plan is the
    order suggested by the overlap check, e.g. ["mosaic", "rotation"]; a plan that
    starts with "sfm" means the set mixes rotation and camera movement, which needs
    structure from motion and is not handled here yet.
    """
    set_dir = Path(set_dir)
    if model == 'sfm':
        raise StitchError('structure-from-motion stitching is planned but not built yet')
    if model not in MODELS:
        raise StitchError(f'unknown model {model}')
    plan = [m for m in plan or [] if m]
    if model == 'auto' and plan and plan[0] == 'sfm':
        raise StitchError('overlap check says this set mixes rotation and camera movement; it needs the structure-from-motion path (planned), or pick a model by hand')
    manifest = json.loads((set_dir / 'manifest.json').read_text(encoding='utf-8'))
    count = len(manifest)
    run = Runner(set_dir / 'stitch.log')
    (set_dir / 'stitch.log').write_text('', encoding='utf-8')
    t0 = time.time()
    write_project(manifest, set_dir / 'start.pto')
    find_args = ['--ncores', threads, '-o', 'points.pto']
    if multirow or count >= 15:
        find_args.append('--multirow')
    if celeste:
        find_args.append('--celeste')
    if ransac:
        find_args.append(f'--ransacmode={ransac}')
    run('cpfind', *find_args, 'start.pto', cwd=set_dir)
    raw_pairs = control_points(set_dir / 'points.pto')
    run('cpclean', '-o', 'clean.pto', 'points.pto', cwd=set_dir)
    try:
        run('autooptimiser', '-a', '-m', '-l', '-s', '-o', 'auto.pto', 'clean.pto', cwd=set_dir)
    except StitchError:
        # photometric optimisation loads the images and can run large sets out of
        # memory; fall back to geometry only so big sets still align
        run('autooptimiser', '-a', '-l', '-s', '-o', 'auto.pto', 'clean.pto', cwd=set_dir)
    auto_error = mean_error(run('checkpto', 'auto.pto', cwd=set_dir, check=False))
    tried = ['rotation', 'mosaic'] if model == 'auto' else [model]
    scores = {}
    for m in tried:
        try:
            scores[m] = solve(set_dir, run, m, auto_error, count)
        except StitchError as err:
            scores[m] = None
            run.steps.append({'failed': m, 'error': str(err)[:300]})
    scores = {m: v for m, v in scores.items() if v}
    if not scores:
        raise StitchError('no camera model could be optimised; see stitch.log')
    prefer = plan[0] if plan else None
    px_scale = max(max(m.get('width') or 0, m.get('height') or 0) for m in manifest) / 1000 or 1.0
    chosen, reason = choose_model(scores, prefer, px_scale)
    shutil.copyfile(set_dir / f'optimised_{chosen}.pto', set_dir / 'optimised.pto')
    relock = [pp['v'] for pp in image_params(set_dir / 'optimised.pto') if pp['v']]
    if relock and (min(relock) < 3 or max(relock) > 175):
        # The lens field of view collapsed during optimisation (too few control
        # points to constrain it, e.g. a long-lens set of distant subjects). Re-solve
        # with the field of view locked to the EXIF value from clean.pto.
        locked = ','.join(v for v in MODEL_VARIABLES.get(chosen, 'y,p,r').split(',') if v != 'v')
        run('pto_var', f'--opt={locked}', '-o', 'relock.pto', 'clean.pto', cwd=set_dir)
        run('autooptimiser', '-n', '-l', '-s', '-o', 'optimised.pto', 'relock.pto', cwd=set_dir)
        scores[chosen]['error_px'] = mean_error(run('checkpto', 'optimised.pto', cwd=set_dir, check=False))
        scores[chosen]['fov_relocked'] = True
    (set_dir / 'full.pto').unlink(missing_ok=True)
    repaired = repair(set_dir, run=run, model=chosen if chosen in MODEL_VARIABLES else None)
    if repaired:
        scores[chosen]['error_px'] = repaired['error_px']
        scores[chosen]['largest_group'] = len(repaired['kept'])
    level(set_dir, run=run)
    turned = upright(set_dir, run=run)
    check = run('checkpto', 'optimised.pto', cwd=set_dir, check=False)
    pairs = control_points(set_dir / 'optimised.pto')
    params = image_params(set_dir / 'optimised.pto')
    used = [manifest[k - 1] for k in repaired['kept']] if repaired else manifest
    layout = describe_layout(used, pairs, params)
    layout['repair'] = repaired
    layout['raw_pairs_with_points'] = len(raw_pairs)
    layout['control_points_raw'] = sum(raw_pairs.values())
    layout['control_points_clean'] = sum(pairs.values())
    layout['pair_points'] = {f'{a + 1}-{b + 1}': n for (a, b), n in sorted(pairs.items())}
    layout['mean_error_px'] = {'auto': auto_error, 'chosen': scores[chosen]['error_px']}
    error = scores[chosen]['error_px']
    poor = error is None or error > POOR_ERROR * px_scale or scores[chosen]['largest_group'] < 0.6 * count
    if poor and 'sfm' in plan:
        reason += '; still a poor fit, the structure-from-motion path is the better route'
    layout['model'] = {'requested': model, 'plan': plan, 'chosen': chosen, 'reason': reason, 'tried': scores, 'poor_fit': poor}
    layout['mosaic'] = chosen == 'mosaic'
    layout['upright'] = turned
    layout['photometric'] = scores[chosen]['photometric']
    layout['kept_solution'] = scores[chosen]['kept_solution']
    layout['checkpto'] = [l for l in check.splitlines() if l.strip()][-6:]
    layout['align_seconds'] = round(time.time() - t0, 1)
    layout['steps'] = run.steps
    write_atomic(set_dir / 'manifest.json', json.dumps(manifest, indent=2))
    write_atomic(set_dir / 'layout.json', json.dumps(layout, indent=2))
    return layout


def lens_owners(pto_path):
    """Images whose line holds a field of view rather than a link to another image."""
    owners = []
    k = 0
    for line in Path(pto_path).read_text(encoding='utf-8', errors='replace').splitlines():
        if line.startswith('i '):
            if re.search(r'\sv-?[\d.]', line):
                owners.append(k)
            k += 1
    return owners


def set_fov(pto_path, fovs):
    """Put the given fields of view (image index -> degrees) back on lens-owning images."""
    lines = Path(pto_path).read_text(encoding='utf-8', errors='replace').splitlines()
    k = 0
    for index, line in enumerate(lines):
        if line.startswith('i '):
            if k in fovs:
                lines[index] = re.sub(r'(\s)v-?[\d.e+-]+', lambda m: f'{m.group(1)}v{fovs[k]:.4f}', line, count=1)
            k += 1
    write_atomic(pto_path, '\n'.join(lines) + '\n')


def resolve(set_dir, run, source, out, model, exif_fov=None):
    """Optimise a project again under one camera model, as solve does.

    The lens is left free; if its field of view collapses, it goes back to the
    EXIF value (exif_fov: image index -> degrees) and stays locked."""
    set_dir = Path(set_dir)
    work = set_dir / 'resolve_tmp.pto'
    run('pto_var', f'--opt={MODEL_VARIABLES[model]}', '-o', work, source, cwd=set_dir)
    run('autooptimiser', '-n', '-l', '-s', '-o', out, work, cwd=set_dir)
    fovs = [pp['v'] for pp in image_params(out) if pp['v']]
    relocked = False
    if fovs and (min(fovs) < 3 or max(fovs) > 175):
        locked = ','.join(v for v in MODEL_VARIABLES[model].split(',') if v != 'v')
        shutil.copyfile(source, work)
        if exif_fov:
            set_fov(work, {k: exif_fov[k] for k in lens_owners(work) if exif_fov.get(k)})
        run('pto_var', f'--opt={locked}', '-o', work, work, cwd=set_dir)
        run('autooptimiser', '-n', '-l', '-s', '-o', out, work, cwd=set_dir)
        relocked = True
    work.unlink(missing_ok=True)
    absolute_paths(out)
    return mean_error(run('checkpto', out, cwd=set_dir, check=False)), relocked


def exif_fovs(set_dir, pto_path):
    """EXIF field of view of each image in a project, from the set's manifest."""
    set_dir = Path(set_dir)
    try:
        manifest = json.loads((set_dir / 'manifest.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    by_name = {Path(m['image']).name: m.get('hfov') for m in manifest}
    frames, _ = parts.frame_info(pto_path)
    return {k: by_name.get(Path(fr['file']).name) for k, fr in enumerate(frames)}


def repair(set_dir, pto='optimised.pto', run=None, model=None):
    """Leave out frames the control points do not hold in place and split off pieces that share nothing.

    See parts.py for the rules. The main part replaces the project and is
    optimised again without the frames left out; every other part of
    parts.MIN_PART_FRAMES or more frames is written as part_<n>.pto, ready to
    become a panorama of its own. The project as it was is kept once as
    full.pto, and a repeat run starts from it. Returns None when every frame
    belongs, otherwise what was done (frame numbers 1-based, as in the
    manifest order of full.pto)."""
    set_dir = Path(set_dir)
    run = run or Runner(set_dir / 'render.log')
    path = set_dir / pto
    full = set_dir / 'full.pto'
    source = full if full.exists() else path
    frames, _ = parts.frame_info(source)
    count = len(frames)
    model = model or ('mosaic' if any(fr['moved'] for fr in frames) else 'rotation')
    px_scale = parts.median([max(fr['w'] or 0, fr['h'] or 0) for fr in frames]) / 1000 or 1.0
    found = parts.split(source, px_scale)
    for f in set_dir.glob('part_*.pto'):
        f.unlink()
    if len(found['parts']) == 1 and len(found['parts'][0]) == count:
        if source == full:
            shutil.copyfile(full, path)
            full.unlink()
        return None
    if source != full:
        shutil.copyfile(path, full)
    fov = exif_fovs(set_dir, full)
    dropped = {k: why for k, why in found['dropped'].items()}
    kept = found['parts'][0] if found['parts'] else []
    if len(kept) < 2:
        raise StitchError('no group of frames is held together by enough control points')
    candidate = set_dir / 'repair_tmp.pto'
    error = relocked = None
    for _ in range(3):
        parts.subset(full, kept, candidate)
        error, relocked = resolve(set_dir, run, candidate, candidate, model,
                                  {n: fov.get(k) for n, k in enumerate(kept)})
        again = parts.split(candidate, px_scale)
        if len(again['parts']) == 1 and len(again['parts'][0]) == len(kept):
            break
        for n, why in again['dropped'].items():
            dropped[kept[n]] = why + ' (after the others were solved)'
        if not again['parts'] or len(again['parts'][0]) < 2:
            raise StitchError('no group of frames is held together by enough control points')
        kept = [kept[n] for n in again['parts'][0]]
    os.replace(candidate, path)
    others = []
    for number, part in enumerate(found['parts'][1:], 2):
        part_file = set_dir / f'part_{number}.pto'
        parts.subset(full, part, part_file)
        part_error, _ = resolve(set_dir, run, part_file, part_file, model, {n: fov.get(k) for n, k in enumerate(part)})
        others.append({'file': part_file.name, 'frames': [k + 1 for k in part], 'error_px': part_error})
    return {'kept': [k + 1 for k in kept], 'left_out': {str(k + 1): why for k, why in sorted(dropped.items())
                                                        if k not in kept},
            'parts': others, 'error_px': error, 'fov_relocked': relocked, 'model': model,
            'swing': parts.swing(path)}


MAX_LEVEL_TILT = 25.0


def level(set_dir, pto='optimised.pto', run=None, tolerance=1.0):
    """Turn the set so the up direction its frames agree on points straight up.

    autooptimiser -l does this while solving; this puts it right again on a
    project that was turned afterwards. A larger tilt than MAX_LEVEL_TILT is
    left to upright, which knows about sets shot looking up or down. Returns
    the tilt corrected, in degrees, or None when the set was level already or
    its frames do not define up."""
    set_dir = Path(set_dir)
    run = run or Runner(set_dir / 'render.log')
    path = set_dir / pto
    frames, _ = parts.frame_info(path)
    if any(parts.focal_px(fr) is None for fr in frames):
        return None
    up = parts.level_up(frames)
    if up is None:
        return None
    yaw, tilt = parts.level_turn(up)
    if tilt < tolerance or tilt > MAX_LEVEL_TILT:
        return None
    probe = set_dir / 'level_tmp.pto'
    best = set_dir / 'level_best.pto'
    shutil.copyfile(path, best)
    left = tilt
    for _ in range(3):
        up = parts.level_up(parts.frame_info(best)[0])
        if up is None:
            break
        yaw, now = parts.level_turn(up)
        if now < 0.2:
            break
        improved = False
        for turn, pitch in ((yaw, now), (yaw, -now), (-yaw, now), (-yaw, -now)):
            run('pano_modify', f'--rotate={turn:.3f},0,0', '-o', probe, best)
            run('pano_modify', f'--rotate=0,{pitch:.3f},0', '-o', probe, probe)
            run('pano_modify', f'--rotate={-turn:.3f},0,0', '-o', probe, probe)
            absolute_paths(probe)
            after = parts.level_up(parts.frame_info(probe)[0])
            remaining = parts.level_turn(after)[1] if after else 180.0
            if remaining < left - 0.05:
                left = remaining
                os.replace(probe, best)
                improved = True
                break
        if not improved:
            break
    probe.unlink(missing_ok=True)
    if left > tilt - 0.5:
        best.unlink()
        return None
    os.replace(best, path)
    return round(tilt, 1)


ORIENTATIONS = ('level', 'centred', 'vertical')


def mean_direction(pto_path, spread=False):
    """Yaw and pitch of the average viewing direction of all images.

    With spread=True also returns how tightly the images bunch around it: 1 when
    they all point the same way, near 0 for a full circle or sphere."""
    x = y = z = 0.0
    images = image_params(pto_path)
    for image in images:
        yaw, pitch = math.radians(image['y']), math.radians(image['p'])
        x += math.cos(pitch) * math.cos(yaw)
        y += math.cos(pitch) * math.sin(yaw)
        z += math.sin(pitch)
    direction = math.degrees(math.atan2(y, x)), math.degrees(math.atan2(z, math.hypot(x, y)))
    if spread:
        return (*direction, math.sqrt(x * x + y * y + z * z) / max(1, len(images)))
    return direction


def angle_diff(a, b):
    return ((a - b + 180) % 360) - 180


def image_files(pto_path):
    """Pixel size and file of every image in a project."""
    pto_path = Path(pto_path)
    files = []
    for line in pto_path.read_text(encoding='utf-8', errors='replace').splitlines():
        if line.startswith('i '):
            files.append({'w': int(re.search(r'\sw(\d+)', line).group(1)),
                          'h': int(re.search(r'\sh(\d+)', line).group(1)),
                          'n': pto_path.parent / re.search(r' n"([^"]+)"', line).group(1)})
    return files


def upness(path):
    """How much each quarter turn of a photo looks the right way up.

    Returns four scores, for the photo as saved and turned 90, 180 and 270 degrees
    anticlockwise (numpy rot90 order). A frame scores high when its top is brighter
    and bluer than its bottom: sky, ceilings and lit backgrounds sit above darker
    ground almost everywhere. Weak on its own, but enough to settle which copy of a
    photo is standing up when frames in one set disagree by a quarter turn."""
    import numpy as np
    from PIL import Image
    with Image.open(path) as img:
        img.draft('RGB', (256, 256))
        a = np.asarray(img.convert('RGB').resize((96, 96)), dtype=np.float32) / 255
    scores = []
    for k in range(4):
        b = np.rot90(a, k)
        value = b.mean(axis=2)
        blue = b[..., 2] - (b[..., 0] + b[..., 1]) / 2
        top, bottom = slice(0, 29), slice(67, 96)
        scores.append(float(value[top].mean() - value[bottom].mean() + blue[top].mean() - blue[bottom].mean()))
    return scores


# A rival group of frames a quarter or half turn from the majority makes a split
# once it holds this share of the set. A smaller one only wins when the content
# favours it by this much per frame.
RIVAL_SHARE = 0.3
DECISIVE_CONTENT = 0.15
QUARTER_SLACK = 20


def roll_group(rolls, centre, tolerance):
    members = [angle_diff(r, centre) for r in rolls if abs(angle_diff(r, centre)) <= tolerance]
    return angle_diff(centre + sorted(members)[len(members) // 2], 0), len(members)


def main_roll_group(rolls, tolerance):
    groups = [roll_group(rolls, r, tolerance) for r in rolls]
    return max(groups, key=lambda g: (g[1], -abs(g[0])))


def choose_turn(rolls, scores, portrait, tolerance=45):
    """Pick the roll that stands the set up, from the frames' own rolls.

    rolls are the frame rolls in the panorama, scores their upness() and portrait
    whether each frame is taller than wide. Albums often hold both a sideways
    original and a copy someone rotated, and both stitch to the same spot. When a
    set mixes landscape and portrait frames, the portrait ones are the turned
    copies (cameras save landscape) and decide. Otherwise the largest group of
    agreeing rolls decides, unless a rival group a quarter or half turn away is big
    enough to make it a split, or the content clearly favours it. The content check
    only ever chooses between rolls that frames actually have.

    Returns the turn, the share of frames that agree with it and what decided it."""
    n = len(rolls)
    tall = [r for r, t in zip(rolls, portrait) if t]
    if tall and len(tall) < n:
        turn, _ = main_roll_group(tall, tolerance)
        _, count = roll_group(rolls, turn, tolerance)
        return turn, count / n, 'portrait'
    major, count = main_roll_group(rolls, tolerance)
    def quarter(r):
        off = abs(angle_diff(r, major))
        return off > tolerance and abs(off - 90 * round(off / 90)) <= QUARTER_SLACK

    rivals = [roll_group(rolls, r, tolerance) for r in rolls if quarter(r)]
    if not rivals:
        return major, count / n, 'majority'
    rival, rival_count = max(rivals, key=lambda g: (g[1], -abs(g[0])))

    def content(centre):
        total = 0.0
        for roll, score in zip(rolls, scores):
            k = min(range(4), key=lambda k: abs(angle_diff(roll + 90 * k, centre)))
            if abs(angle_diff(roll + 90 * k, centre)) <= tolerance:
                total += score[k]
        return total

    lead = content(rival) - content(major)
    if lead > 0 and (rival_count / n >= RIVAL_SHARE or lead / n >= DECISIVE_CONTENT):
        return rival, rival_count / n, 'content'
    return major, count / n, 'majority'


COMPACT = 0.8
TIPPED_PITCH = 45
ROLL_TOLERANCE = 45
WIDE_SNAP = 20


def upright(set_dir, pto='optimised.pto', run=None, dry_run=False):
    """Turn a set that the optimiser left tipped over, sideways or upside down upright.

    autooptimiser -l levels a panorama by finding the plane the image horizons share.
    When the images barely turn (a few frames, or a narrow sweep) that plane is not
    defined and it can take the viewing direction for "up": the set then points at
    the zenith or nadir with every frame rolled, and renders upside down or on its
    side, with a bogus 360 degree extent. For a compact set, rotate it so the average
    viewing direction is on the horizon and the frames stand the way most of them
    agree.

    Frames can also disagree by a quarter turn. Old cameras saved portrait shots
    sideways, and albums often hold both that original and a copy someone rotated
    ("IMG_1(rev 1)", "DSCN2 (2)", an edit at a smaller size). Both copies stitch to
    the same spot, so a plain vote between their rolls is a coin toss. choose_turn
    settles it from the portrait frames and, failing that, the frame content.

    A wide set is turned when it is upside down as a whole, or when its frames agree
    it lies on its side.

    Rewrites the project in place and returns the correction, or None if the set
    was already upright. dry_run only reports the correction."""
    set_dir = Path(set_dir)
    run = run or Runner(set_dir / 'render.log')
    path = set_dir / pto
    yaw, pitch, spread = mean_direction(path, spread=True)
    compact = spread >= COMPACT
    tipped = compact and abs(pitch) > TIPPED_PITCH
    probe = set_dir / ('upright_check.pto' if dry_run else 'upright.pto')
    run('pano_modify', f'--rotate={-yaw:.3f},0,0', '-o', probe, path)
    if compact:
        run('pano_modify', f'--rotate=0,{-pitch:.3f},0', '-o', probe, probe)
    rolls = [image['r'] for image in image_params(probe)]
    files = image_files(probe)
    scores = []
    for f in files:
        try:
            scores.append(upness(f['n']))
        except OSError:
            scores.append([0.0, 0.0, 0.0, 0.0])
    roll, share, basis = choose_turn(rolls, scores, [f['h'] > f['w'] for f in files], ROLL_TOLERANCE)
    agreed = share > 0.5 or basis != 'majority'
    snapped = 90 * round(roll / 90)
    if compact and (tipped or abs(roll) > ROLL_TOLERANCE):
        turn = roll
    elif not compact and agreed and snapped and abs(roll - snapped) <= WIDE_SNAP:
        turn = float(snapped)
    else:
        probe.unlink()
        return None
    correction = {'yaw': round(yaw, 1), 'pitch': round(pitch, 1), 'roll': round(turn, 1),
                  'spread': round(spread, 2), 'tipped': tipped, 'basis': basis}
    if dry_run:
        probe.unlink()
        return correction
    run('pano_modify', f'--rotate=0,0,{-turn:.3f}', '-o', probe, probe)
    absolute_paths(probe)
    os.replace(probe, path)
    return correction


def default_orientation(pto_path):
    _, pitch = mean_direction(pto_path)
    if abs(pitch) < 20:
        return 'level'
    return 'centred'


def preview_name(projection, orientation):
    if orientation == 'level':
        return projection
    return f'{projection}-{orientation}'


def render_full(set_dir, name, log=None):
    """Render a reviewed view again at the photos' own resolution, as far as the
    memory budget and the megapixel cap allow, into full/<name>.jpg and
    full/<name>_crop.jpg. Starts from previews/<name>.pto, the project the preview
    was made from, so the result is the same picture, only larger. Kept until the
    preview is made again. Returns the full folder."""

    set_dir = Path(set_dir)
    source = set_dir / 'previews' / f'{name}.pto'
    out_dir = set_dir / 'full'
    done = [out_dir / f'{name}.jpg', out_dir / f'{name}_crop.jpg']
    if not source.exists():
        raise StitchError(f'no project for {name}')
    if all(f.exists() and f.stat().st_mtime >= source.stat().st_mtime for f in done):
        return out_dir
    orientation = 'vertical' if name.endswith('-vertical') else 'level'
    renders_file = set_dir / 'previews' / 'renders.json'
    renders = json.loads(renders_file.read_text(encoding='utf-8')) if renders_file.exists() else {}
    fuse = (renders.get(name) or {}).get('fused_stacks') is not None
    tmp = set_dir / 'full_tmp'
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    out_dir.mkdir(exist_ok=True)
    if log:
        log(f'{set_dir.name}: making {name} at full size, which can take several minutes')
    with Progress(f'full-size render of {set_dir.name}', total=4, unit='steps', every=1.0) as shown:
        return _render_full(set_dir, name, source, out_dir, done, fuse, orientation, tmp, shown, log)


def _render_full(set_dir, name, source, out_dir, done, fuse, orientation, tmp, shown, log):
    from . import limits

    def step(what=None):
        shown.update()
        if log and what:
            log(f'{set_dir.name}: {name} at full size, {what} ({shown.done} of {shown.total})')

    run = Runner(set_dir / 'render.log')
    t0 = time.time()
    work = tmp / 'work.pto'
    shutil.copy(source, work)
    run('pano_modify', '--canvas=AUTO', '-o', work, work)
    p = pano_line(work)
    outside = tmp / 'outside.pto'
    run('pano_modify', '--crop=AUTOOUTSIDE', '-o', outside, work)
    left, right, top, bottom = pano_line(outside)['crop']
    scale = limits.full_scale(right - left, bottom - top)
    canvas_w, canvas_h = max(1, round(p['w'] * scale)), max(1, round(p['h'] * scale))
    run('pano_modify', f'--canvas={canvas_w}x{canvas_h}', '--crop=AUTOOUTSIDE', '-o', work, work)
    outer = pano_line(work)['crop']
    inside = tmp / 'inside.pto'
    run('pano_modify', '--crop=AUTO', '-o', inside, work)
    inner = pano_line(inside)['crop']
    step(f'{canvas_w} x {canvas_h} canvas')
    nona_args = ['--ignore-exposure'] if fuse else []
    run('nona', *nona_args, '-m', 'TIFF_m', '-o', tmp / 'layer', work)
    layers = sorted(tmp.glob('layer*.tif'))
    step(f'{len(layers)} photos remapped')
    flat, cropped, *_ = blend(run, tmp, work, layers, outer, inner, orientation, fuse)
    step('blended')
    for image, target in ((cropped, done[1]), (flat, done[0])):
        image.save(tmp / target.name, quality=95, subsampling=0)
        os.replace(tmp / target.name, target)
    shutil.rmtree(tmp)
    step()
    if log:
        note = '' if scale >= 1 else f', {scale:.0%} of full size to fit the memory or the megapixel cap'
        log(f'{set_dir.name}: {name} at {flat.width} x {flat.height}{note}, {time.time() - t0:.0f} s')
    return out_dir


def coverage(set_dir, pto='optimised.pto'):
    """Horizontal and vertical extent of an aligned set in degrees."""
    set_dir = Path(set_dir)
    run = Runner(set_dir / 'render.log')
    probe = set_dir / 'coverage.pto'
    run('pano_modify', '--projection=2', '--fov=AUTO', '-o', probe, set_dir / pto)
    p = pano_line(probe)
    probe.unlink()
    return round(p['v'], 1), round(p['v'] * p['h'] / max(1, p['w']), 1)


RECTILINEAR_MAX = 100
CYLINDRICAL_MAX = 200
TALL = 110


def auto_projection(hfov, vfov):
    if hfov > CYLINDRICAL_MAX or vfov > TALL:
        return 'equirectangular'
    if hfov > RECTILINEAR_MAX or vfov > RECTILINEAR_MAX:
        return 'cylindrical'
    return 'rectilinear'


def exposure_stacks(pto_path, spread=0.15):
    """Group images shot from the same direction (exposure brackets) into stacks."""
    params = image_params(pto_path)
    stacks = []
    for index, image in enumerate(params):
        yaw, pitch = math.radians(image['y']), math.radians(image['p'])
        vec = (math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch))
        for stack in stacks:
            ref = stack['vec']
            angle = math.degrees(math.acos(max(-1.0, min(1.0, sum(a * b for a, b in zip(vec, ref))))))
            if angle < spread * min(stack['hfov'], image['v'] or 50):
                stack['images'].append(index)
                break
        else:
            stacks.append({'vec': vec, 'hfov': image['v'] or 50, 'images': [index]})
    return [s['images'] for s in stacks]


def studio_background_share(image_path):
    """Fraction of pixels that look like a chroma-key backdrop: bright, strongly
    saturated green or blue. A high share points to a studio subject, not scenery."""
    import numpy as np
    from PIL import Image
    with Image.open(image_path) as img:
        hsv = img.convert('RGB').resize((160, 160)).convert('HSV')
    h, s, v = (np.asarray(hsv, dtype=np.float32)[..., i] for i in range(3))
    hue = h * 360 / 255
    keyed = (s > 100) & (v > 70) & (hue >= 110) & (hue <= 165)
    return float(keyed.mean())


def blend(run, tmp, work, layers, outer, inner, orientation, fuse):
    """Fuse and blend the remapped layers, then cut the outer and inner crops.
    Returns (flat, cropped, rgba, seams, blender, dropped, stack_count)."""
    from PIL import Image
    if not layers:
        raise StitchError('nona produced no images')
    stack_count = None
    if fuse:
        by_index = {int(re.search(r'(\d+)$', f.stem).group(1)): f for f in layers}
        fused = []
        for number, stack in enumerate(exposure_stacks(work)):
            files = [by_index[i] for i in stack if i in by_index]
            if len(files) == 1:
                fused.append(files[0])
                continue
            if not files:
                continue
            out = tmp / f'stack{number:03d}.tif'
            run('enfuse', '--compression=lzw', '-o', out, *files)
            fused.append(out)
        stack_count = len(fused)
        layers = fused
    blended = tmp / 'blend.tif'
    blender, dropped, seams = 'single', 0, None
    if len(layers) > 1:
        # Duplicates and zoom layers inside wider frames go to blend_layers, which
        # puts the sharpest frames on top; enblend handles plain overlaps.
        contained = False
        detail = layer_detail(layers, image_params(work), image_files(work))
        if not fuse:
            by_detail = dict(zip(layers, detail))
            layers, dropped, contained = plan_layers(layers, detail)
            detail = [by_detail[f] for f in layers]
        blender = 'enblend' if len(layers) > 1 else 'single'
        if contained and len(layers) > 1:
            blender = 'layered'
        elif len(layers) > 1:
            try:
                run('enblend', '--compression=lzw', '-o', blended, *layers)
            except StitchError:
                blender = 'layered'
        if blender == 'layered':
            if fuse:
                layers = sorted(layers)
                detail = [1.0] * len(layers)
            blended = tmp / 'blend.png'
            seams = blend_layers(layers, blended, detail)
    if len(layers) == 1:
        shutil.copyfile(layers[0], blended)
    with Image.open(blended) as img:
        rgba = img.convert('RGBA')
    background = Image.new('RGBA', rgba.size, (24, 24, 24, 255))
    flat = Image.alpha_composite(background, rgba).convert('RGB')
    ox, oy = outer[0], outer[2]
    if flat.size != (outer[1] - outer[0], outer[3] - outer[2]):
        flat = flat.crop((outer[0], outer[2], outer[1], outer[3]))
    box = (max(0, inner[0] - ox), max(0, inner[2] - oy),
           min(flat.width, inner[1] - ox), min(flat.height, inner[3] - oy))
    cropped = flat.crop(box)
    if orientation == 'vertical':
        flat = flat.rotate(-90, expand=True)
        cropped = cropped.rotate(-90, expand=True)
    return flat, cropped, rgba, seams, blender, dropped, stack_count


def render(set_dir, projection, orientation='level', width=3000,
           pto='optimised.pto', max_rect_fov=140, fuse=False):
    """Render one projection.

    level keeps the horizon level, centred tilts the view so the middle of
    the set sits on the horizon (for sets shot looking up or down), and
    vertical also turns the panorama on its side so cylindrical projections
    wrap top to bottom. Writes <name>.jpg (bounding box of all images) and
    <name>_crop.jpg (largest rectangle with no empty corners).

    fuse treats the set as exposure brackets: images from the same direction
    are exposure fused (enfuse) first, then the stacks are blended."""
    from PIL import Image
    set_dir = Path(set_dir)
    out_dir = set_dir / 'previews'
    tmp = set_dir / 'render_tmp'
    out_dir.mkdir(exist_ok=True)
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    run = Runner(set_dir / 'render.log')
    t0 = time.time()
    number = PROJECTIONS[projection]
    work = tmp / 'work.pto'
    yaw, pitch = mean_direction(set_dir / pto)
    shutil.copy(set_dir / pto, work)
    absolute_paths(work, base=set_dir)
    run('pano_modify', f'--rotate={-yaw:.3f},0,0', '-o', work, work)
    if orientation in ('centred', 'vertical'):
        run('pano_modify', f'--rotate=0,{-pitch:.3f},0', '-o', work, work)
        if orientation == 'vertical':
            run('pano_modify', '--rotate=0,0,90', '-o', work, work)
    run('pano_modify', f'--projection={number}', '--fov=AUTO', '--canvas=AUTO', '-o', work, work)
    p = pano_line(work)
    if number == 0:
        vfov = 2 * math.degrees(math.atan(math.tan(math.radians(min(p['v'], 179) / 2)) * p['h'] / p['w']))
        if p['v'] > max_rect_fov or vfov > max_rect_fov:
            run('pano_modify', f'--fov={min(p["v"], max_rect_fov):.1f}x{min(vfov, max_rect_fov):.1f}',
                '--canvas=AUTO', '-o', work, work)
    p = pano_line(work)
    outside = tmp / 'outside.pto'
    run('pano_modify', '--crop=AUTOOUTSIDE', '-o', outside, work)
    left, right, top, bottom = pano_line(outside)['crop']
    scale = width / max(1, (bottom - top) if orientation == 'vertical' else (right - left))
    canvas_w, canvas_h = max(1, round(p['w'] * scale)), max(1, round(p['h'] * scale))
    run('pano_modify', f'--canvas={canvas_w}x{canvas_h}', '--crop=AUTOOUTSIDE', '-o', work, work)
    outer = pano_line(work)['crop']
    inside = tmp / 'inside.pto'
    run('pano_modify', '--crop=AUTO', '-o', inside, work)
    inner = pano_line(inside)['crop']
    nona_args = ['--ignore-exposure'] if fuse else []
    run('nona', *nona_args, '-m', 'TIFF_m', '-o', tmp / 'layer', work)
    layers = sorted(tmp.glob('layer*.tif'))
    if not layers:
        # A steep tilt can make a rectilinear or cylindrical projection degenerate:
        # the images fall outside the canvas and nona has nothing to render. Fall
        # back to equirectangular, which stays bounded for any orientation, and
        # rebuild the crops at the target width.
        run('pano_modify', '--projection=2', '--fov=AUTO', '--canvas=AUTO', '-o', work, work)
        p = pano_line(work)
        run('pano_modify', '--crop=AUTOOUTSIDE', '-o', outside, work)
        left, right, top, bottom = pano_line(outside)['crop']
        scale = width / max(1, (bottom - top) if orientation == 'vertical' else (right - left))
        canvas_w, canvas_h = max(1, round(p['w'] * scale)), max(1, round(p['h'] * scale))
        run('pano_modify', f'--canvas={canvas_w}x{canvas_h}', '--crop=AUTOOUTSIDE', '-o', work, work)
        outer = pano_line(work)['crop']
        run('pano_modify', '--crop=AUTO', '-o', inside, work)
        inner = pano_line(inside)['crop']
        run('nona', *nona_args, '-m', 'TIFF_m', '-o', tmp / 'layer', work)
        layers = sorted(tmp.glob('layer*.tif'))
    flat, cropped, rgba, seams, blender, dropped, stack_count = blend(run, tmp, work, layers, outer, inner,
                                                                       orientation, fuse)
    name = preview_name(projection, orientation)
    cover = rgba.getchannel('A')
    seam_mask = Image.fromarray(seams) if seams is not None else None
    if cover.size != (outer[1] - outer[0], outer[3] - outer[2]):
        cover = cover.crop((outer[0], outer[2], outer[1], outer[3]))
        seam_mask = seam_mask.crop((outer[0], outer[2], outer[1], outer[3])) if seam_mask else None
    if orientation == 'vertical':
        cover = cover.rotate(-90, expand=True)
        seam_mask = seam_mask.rotate(-90, expand=True) if seam_mask else None
    for image, target in ((cropped, f'{name}_crop.jpg'), (flat, f'{name}.jpg')):
        image.save(tmp / target, quality=90)
        os.replace(tmp / target, out_dir / target)
    # where real pixels are, and where layers were faded together: the AI fill
    # only invents pixels outside the first and may smooth the second
    for image, target in ((cover, f'{name}_cover.png'), (seam_mask, f'{name}_seams.png')):
        if image is None:
            (out_dir / target).unlink(missing_ok=True)
            continue
        image.save(tmp / target)
        os.replace(tmp / target, out_dir / target)
    # an AI fill belongs to the render it was made from
    for target in (f'{name}_fill.jpg', f'{name}_fillshow.jpg', f'{name}_fill_mask.png'):
        (out_dir / target).unlink(missing_ok=True)
    shutil.copy(work, tmp / f'{name}.pto')
    os.replace(tmp / f'{name}.pto', out_dir / f'{name}.pto')
    shutil.rmtree(tmp)
    info = {
        'projection': projection,
        'orientation': orientation,
        'size': list(flat.size),
        'crop_size': list(cropped.size),
        'fov': p['v'],
        'fused_stacks': stack_count,
        'blender': blender,
        'dropped_duplicates': dropped,
        'seconds': round(time.time() - t0, 1),
        'peak_mb': max(s['peak_mb'] for s in run.steps),
    }
    index_file = out_dir / 'renders.json'
    renders = json.loads(index_file.read_text(encoding='utf-8')) if index_file.exists() else {}
    renders[name] = info
    write_atomic(index_file, json.dumps(renders, indent=2))
    record_file = set_dir / 'batch.json'
    if record_file.exists():
        record = json.loads(record_file.read_text(encoding='utf-8'))
        if name in record.get('fill', {}):
            del record['fill'][name]
            write_atomic(record_file, json.dumps(record, indent=1))
    return info
