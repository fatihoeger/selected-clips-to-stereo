"""Selected Clips to Stereo, version 1.0
Copyright (c) 2026 Fatih Öger <fatihoger@colorwise.at>. All rights reserved.

Terms: free to use. Do not modify or redistribute this script; get copies
only from the author. Each run checks that this copy is the official,
unmodified version and reports the result in the console and the log.
Only the settings in the marked section below may be changed.

Run inside DaVinci Resolve: Workspace > Scripts > Edit.

Average the embedded audio channels of the selected recordings into identical
L/R channels. Silent channels (disarmed or unused tracks) are left out of the
average, channels with inverted polarity are flipped, the mix is raised to
the source's peak level, the source BWF timecode is carried over, and the
results are imported into a new "Stereo" bin inside the current bin.
Requires FFmpeg and FFprobe (macOS: brew install ffmpeg).
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import combinations, product
import json
import math
import os
from pathlib import Path
import re
import hashlib
import shutil
import subprocess
import sys
import traceback
from datetime import datetime
from uuid import uuid4

VERSION = '1.0'
AUTHOR = 'Fatih Öger <fatihoger@colorwise.at>'
# Set by the author's signing tool; identifies the official version.
FINGERPRINT = '678f8771f45301e8ff0e2932cd706761e38406c48e0489bedc67b988a0edc317'

# --- Settings you may change ---
# Optional absolute paths if the tools are not found automatically.
FFMPEG_PATH = ""
FFPROBE_PATH = ""
# Change this if you prefer a different permanent output location.
OUTPUT_ROOT = Path.home() / "Movies" / "Resolve Stereo"
# Created inside the current bin; "Stereo 1", "Stereo 2", ... if already taken.
BIN_NAME = "Stereo"
# --- End of settings ---

SAMPLE_RATE = 48000
# Channels whose peak never rises above this level are left out of the average.
SKIP_SILENT_CHANNELS = True
SILENCE_THRESHOLD_DB = -60.0
# Flip the polarity of channels that cancel each other out (for example a boom
# and a lav wired in opposite polarity), when that makes the mix louder by more
# than the margin.
FIX_POLARITY = True
POLARITY_MARGIN_DB = 1.0
# Raise each mix so its peak matches the loudest source channel's peak. This
# never clips and keeps the recorder's gain; it never lowers the level.
MATCH_SOURCE_PEAK = True
MAX_GAIN_DB = 12.0
# Warn when the mix is still this much quieter than unrelated channels would
# sum to, which means channels cancel each other out.
PHASE_WARNING_DB = 6.0
# Number of clips converted at the same time.
PARALLEL_JOBS = min(4, os.cpu_count() or 2)

SETTINGS_START = '# --- Settings you may change ---'
SETTINGS_END = '# --- End of settings ---'
# Inside the settings section only these plain values are ignored by the check.
_TEXT = r"""r?(?:"[^"]*"|'[^']*')"""
EDITABLE = re.compile(
    r'^(?:FFMPEG_PATH|FFPROBE_PATH|BIN_NAME)\s*=\s*{0}$|'
    r'^OUTPUT_ROOT\s*=\s*(?:Path\.home\(\)|Path\({0}\))(?:\s*/\s*{0})*$'.format(_TEXT))

STAT_LINE = re.compile(
    r'\[Parsed_astats_(\d+) @ [^\]]*\] (Channel|Peak level dB|RMS level dB): (\S+)')


def items(value):
    return list(value.values()) if isinstance(value, dict) else list(value or [])


def safe_name(value):
    return re.sub(r'[^\w. -]+', '_', str(value)).strip(' .')[:90] or 'Clip'


def fingerprint(text):
    """SHA-256 of the script, leaving out the FINGERPRINT line, comments and
    blank lines in the settings section, and allowed setting values."""
    kept, in_settings = [], False
    for line in text.replace('\r\n', '\n').split('\n'):
        stripped = line.strip()
        if stripped.startswith('FINGERPRINT = '):
            continue
        if stripped == SETTINGS_START:
            in_settings = True
        elif stripped == SETTINGS_END:
            in_settings = False
        elif in_settings and (not stripped or stripped.startswith('#') or
                              EDITABLE.match(stripped)):
            continue
        kept.append(line.rstrip())
    return hashlib.sha256('\n'.join(kept).encode('utf-8')).hexdigest()


def script_files():
    """This script's own file. Resolve does not always say where a menu script
    lives, so fall back to looking in Resolve's script folders."""
    for name in (globals().get('__file__'), sys.argv[0] if sys.argv else None):
        if name and str(name).endswith('.py') and Path(name).is_file():
            return [Path(name)]
    home = Path.home()
    roots = [Path('/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts'),
             home / 'Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts',
             Path('/opt/resolve/Fusion/Scripts'),
             home / '.local/share/DaVinciResolve/Fusion/Scripts']
    for variable, rest in (('PROGRAMDATA', 'Blackmagic Design/DaVinci Resolve/Fusion/Scripts'),
                           ('APPDATA', 'Blackmagic Design/DaVinci Resolve/Support/Fusion/Scripts')):
        if os.environ.get(variable):
            roots.append(Path(os.environ[variable]) / rest)
    return [path for root in roots if root.is_dir()
            for path in root.glob('*/Selected_Clips_to_Stereo*.py')]


def integrity():
    """'official', 'modified' or 'unverified' (own file not found)."""
    verdicts = set()
    for path in script_files():
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        if "VERSION = '{}'".format(VERSION) in text:
            verdicts.add('official' if FINGERPRINT and
                         fingerprint(text) == FINGERPRINT else 'modified')
    return verdicts.pop() if len(verdicts) == 1 else 'unverified'


def find_tool(name, configured):
    candidates = [configured, shutil.which(name)]
    for directory in ('/opt/homebrew/bin', '/usr/local/bin', '/usr/bin',
                      str(Path.home() / 'bin')):
        candidates.append(str(Path(directory) / name))
    if os.name == 'nt':
        candidates.append(str(Path('C:/ffmpeg/bin') / (name + '.exe')))
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise RuntimeError(
        '{} was not found. Install FFmpeg (including ffprobe), or set its '
        'absolute path at the top of this script. See README.md.'.format(name))


def run_command(args, timeout=None):
    options = {'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE,
               'encoding': 'utf-8', 'errors': 'replace'}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(args, timeout=timeout, **options)
    if result.returncode:
        raise RuntimeError(result.stderr[-4000:] or 'Command failed.')
    return result


def probe(path, ffprobe):
    return json.loads(run_command([
        ffprobe, '-v', 'error', '-show_streams', '-show_format',
        '-of', 'json', str(path)], timeout=60).stdout)


def audio_streams(info):
    return [s for s in info.get('streams', []) if s.get('codec_type') == 'audio']


def db(value):
    return 20 * math.log10(value) if value > 0 else float('-inf')


def parse_astats(log):
    """Per-channel {'peak', 'rms'} in dB for each astats filter, in graph order."""
    found = {}
    for number, key, value in STAT_LINE.findall(log):
        channels = found.setdefault(int(number), [])
        if key == 'Channel':
            channels.append({})
        elif channels:
            channels[-1]['peak' if key.startswith('Peak') else 'rms'] = float(value)
    # ffmpeg numbers filters across the whole graph, so only the order is useful.
    return [found[number] for number in sorted(found)]


def measure_channels(source, streams, ffmpeg):
    """Peak and RMS level of every channel, one list per audio stream."""
    graph = ';'.join(
        '[0:{}]astats=measure_overall=none:measure_perchannel=Peak_level+RMS_level'
        '[m{}]'.format(stream['index'], i) for i, stream in enumerate(streams))
    command = [ffmpeg, '-hide_banner', '-nostdin', '-nostats', '-loglevel', 'info',
               '-i', str(source), '-filter_complex', graph]
    for i in range(len(streams)):
        command.extend(['-map', '[m{}]'.format(i)])
    command.extend(['-f', 'null', '-'])
    found = parse_astats(run_command(command).stderr)
    levels = []
    for i, stream in enumerate(streams):
        channels = found[i] if i < len(found) else []
        if len(channels) != int(stream.get('channels', 0)):
            raise RuntimeError('Could not measure the channel levels.')
        levels.append(channels)
    return levels


def choose_channels(streams, levels):
    """The channels to average, each as a dict with its stream index, channel
    number inside the stream, 1-based overall number and levels; plus the
    overall numbers of the channels left out as silent."""
    every = []
    for stream, channels in zip(streams, levels):
        for c, level in enumerate(channels):
            every.append(dict(level, stream=stream['index'], channel=c,
                              number=len(every) + 1, sign=1))
    active = [ch for ch in every
              if not SKIP_SILENT_CHANNELS or ch['peak'] > SILENCE_THRESHOLD_DB]
    if not active:
        # Everything is silent: average all channels rather than fail.
        return every, []
    chosen = {ch['number'] for ch in active}
    return active, [ch['number'] for ch in every if ch['number'] not in chosen]


def gather(active):
    """Graph parts that put the chosen channels into one stream labelled [act],
    in the order of the active list."""
    by_stream = []
    for ch in active:
        if not by_stream or by_stream[-1][0] != ch['stream']:
            by_stream.append((ch['stream'], []))
        by_stream[-1][1].append(ch['channel'])
    parts = []
    for i, (index, channels) in enumerate(by_stream):
        label = 'act' if len(by_stream) == 1 else 's{}'.format(i)
        parts.append('[0:{}]aresample={},pan={} channels|{},asetpts=PTS-STARTPTS[{}]'.format(
            index, SAMPLE_RATE, len(channels),
            '|'.join('c{}=c{}'.format(n, c) for n, c in enumerate(channels)), label))
    if len(by_stream) > 1:
        parts.append('{}amerge=inputs={}[act]'.format(
            ''.join('[s{}]'.format(i) for i in range(len(by_stream))), len(by_stream)))
    return parts


def weighted_sum(terms):
    """pan expression for [(gain, channel)] terms, e.g. 0.5*c0-0.5*c1."""
    text = ''.join('{:+.15g}*c{}'.format(gain, c) for gain, c in terms)
    return text[1:] if text.startswith('+') else text


def fix_polarity(source, active, ffmpeg):
    """Flip channels that cancel each other out (e.g. a boom and a lav wired in
    opposite polarity). Measures every pair added and subtracted, then picks
    the polarities that give the loudest mix; the loudest channel never flips."""
    count = len(active)
    if not FIX_POLARITY or count < 2:
        return []
    pairs = list(combinations(range(count), 2))
    chunks = [pairs[i:i + 16] for i in range(0, len(pairs), 16)]  # 32 outputs per pan
    parts = gather(active)
    parts.append('[act]asplit={}{}'.format(
        len(chunks), ''.join('[a{}]'.format(n) for n in range(len(chunks)))))
    for n, chunk in enumerate(chunks):
        terms = []
        for k, (i, j) in enumerate(chunk):
            terms.append('c{}={}'.format(2 * k, weighted_sum([(0.5, i), (0.5, j)])))
            terms.append('c{}={}'.format(2 * k + 1, weighted_sum([(0.5, i), (-0.5, j)])))
        parts.append('[a{0}]pan={1} channels|{2},astats=measure_overall=none:'
                     'measure_perchannel=RMS_level[p{0}]'.format(n, 2 * len(chunk), '|'.join(terms)))
    command = [ffmpeg, '-hide_banner', '-nostdin', '-nostats', '-loglevel', 'info',
               '-i', str(source), '-filter_complex', ';'.join(parts)]
    for n in range(len(chunks)):
        command.extend(['-map', '[p{}]'.format(n)])
    command.extend(['-f', 'null', '-'])
    found = [ch for chunk in parse_astats(run_command(command).stderr) for ch in chunk]
    if len(found) != 2 * len(pairs):
        raise RuntimeError('Could not compare channel polarity.')

    def power(level):
        return 10 ** (level / 10) if level != float('-inf') else 0.0

    # power((i+j)/2) - power((i-j)/2) is the pair's shared signal: positive when
    # they reinforce each other, negative when they cancel.
    shared = {pair: power(found[2 * k]['rms']) - power(found[2 * k + 1]['rms'])
              for k, pair in enumerate(pairs)}

    def mix_power(signs):
        return sum(power(ch['rms']) for ch in active) + 2 * sum(
            signs[i] * signs[j] * value for (i, j), value in shared.items())

    ref = max(range(count), key=lambda i: active[i]['rms'])
    plain = [1] * count
    if count <= 12:
        candidates = ([1 if i == ref else signs[i - (i > ref)] for i in range(count)]
                      for signs in product((1, -1), repeat=count - 1))
        # On a tie, flip as few channels as possible.
        best = max(candidates, key=lambda signs: (mix_power(signs), signs.count(1)))
    else:
        # Too many channels to try every combination: flip one at a time.
        best, improved = list(plain), True
        while improved:
            improved = False
            for i in range(count):
                if i != ref:
                    trial = best[:i] + [-best[i]] + best[i + 1:]
                    if mix_power(trial) > mix_power(best):
                        best, improved = trial, True
    if mix_power(best) <= mix_power(plain) * 10 ** (POLARITY_MARGIN_DB / 10):
        return []
    for ch, sign in zip(active, best):
        ch['sign'] = sign
    return [ch['number'] for ch in active if ch['sign'] < 0]


def make_filter(active, gain_db=0.0, measure=False):
    """Weight each chosen channel equally (with its polarity and the gain),
    including across separate streams. With measure, the graph only measures
    the mono mix ([stats]) instead of producing the stereo output ([out])."""
    if not active:
        raise ValueError('No readable source audio channels.')
    weight = 10 ** (gain_db / 20) / len(active)
    parts = gather(active)
    parts.append('[act]pan=mono|c0={},{}'.format(
        weighted_sum([(ch['sign'] * weight, i) for i, ch in enumerate(active)]),
        'astats=measure_overall=none:measure_perchannel=Peak_level+RMS_level[stats]'
        if measure else 'pan=stereo|c0=c0|c1=c0[out]'))
    return ';'.join(parts)


def measure_mix(source, active, ffmpeg):
    """Peak and RMS level of the mono mix before any gain."""
    found = parse_astats(run_command(
        [ffmpeg, '-hide_banner', '-nostdin', '-nostats', '-loglevel', 'info',
         '-i', str(source), '-filter_complex', make_filter(active, measure=True),
         '-map', '[stats]', '-f', 'null', '-']).stderr)
    if not found or not found[0]:
        raise RuntimeError('Could not measure the mixed level.')
    return found[0][0]


def peak_gain(active, mix_peak_db):
    """Gain in dB that brings the mix peak up to the loudest source channel's."""
    if not MATCH_SOURCE_PEAK or mix_peak_db == float('-inf'):
        return 0.0
    target = max(ch['peak'] for ch in active)
    return min(max(target - mix_peak_db, 0.0), MAX_GAIN_DB)


def phase_warning(active, mix_rms_db):
    """Compare the mix with what unrelated channels would add up to."""
    if len(active) < 2:
        return None
    weight = 1.0 / len(active)
    expected = db(math.sqrt(sum((weight * 10 ** (ch['rms'] / 20)) ** 2
                                for ch in active if ch['rms'] != float('-inf'))))
    if expected == float('-inf') or mix_rms_db >= expected - PHASE_WARNING_DB:
        return None
    return ('Mix is {:.1f} dB quieter than expected; some channels still cancel '
            'each other out.'.format(expected - mix_rms_db))


def check_alignment(streams):
    # Do not silently shift independently timestamped audio streams.
    if len(streams) < 2:
        return
    starts = [s.get('start_time') for s in streams]
    if any(v is None or v == 'N/A' for v in starts):
        raise ValueError('Cannot verify alignment of multiple audio streams; '
                         'mix this clip in a Resolve timeline instead.')
    times = [float(v) for v in starts]
    if max(times) - min(times) > 1.0 / SAMPLE_RATE:
        raise ValueError('Audio streams have different start times; '
                         'mix this clip in a Resolve timeline instead.')


def tag(tags, key):
    return next((v for k, v in (tags or {}).items() if k.lower() == key), None)


def source_time_reference(info, stream):
    """The recorder's BWF timecode in samples since midnight, at SAMPLE_RATE."""
    value = (tag(info.get('format', {}).get('tags'), 'time_reference') or
             tag(stream.get('tags'), 'time_reference'))
    try:
        samples = int(value)
    except (TypeError, ValueError):
        return None
    rate = int(stream.get('sample_rate') or SAMPLE_RATE)
    return (samples * SAMPLE_RATE + rate // 2) // rate


def reject_linked_audio(clip):
    method = getattr(clip, 'GetAudioMapping', None)
    if callable(method):
        raw = method()
        if raw:
            mapping = json.loads(raw) if isinstance(raw, str) else raw
            if mapping.get('linked_audio'):
                raise ValueError('Clip has linked/synced external audio. This script '
                                 'supports embedded source audio only.')


def convert(source, destination, ffmpeg, ffprobe, start_timecode=None):
    """Runs in a worker thread: no Resolve API calls in here."""
    info = probe(source, ffprobe)
    streams = audio_streams(info)
    if not streams:
        raise ValueError('No readable source audio channels.')
    check_alignment(streams)
    levels = measure_channels(source, streams, ffmpeg)
    active, silent = choose_channels(streams, levels)
    flipped = fix_polarity(source, active, ffmpeg)
    mix = measure_mix(source, active, ffmpeg)
    gain = peak_gain(active, mix['peak'])
    graph = make_filter(active, gain)
    time_reference = source_time_reference(info, streams[0])
    partial = destination.with_name(destination.stem + '.partial.wav')
    try:
        command = [ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'error',
                   '-n', '-i', str(source), '-filter_complex', graph,
                   '-map', '[out]', '-map_metadata', '0',
                   '-c:a', 'pcm_s24le', '-rf64', 'auto']
        if time_reference is not None:
            command.extend(['-write_bext', '1',
                            '-metadata', 'time_reference={}'.format(time_reference)])
        if start_timecode:
            command.extend(['-timecode', str(start_timecode)])
        command.append(str(partial))
        run_command(command)
        output_info = probe(partial, ffprobe)
        output = audio_streams(output_info)
        if (len(output) != 1 or int(output[0].get('channels', 0)) != 2 or
                output[0].get('codec_name') != 'pcm_s24le' or
                int(output[0].get('sample_rate', 0)) != SAMPLE_RATE):
            raise RuntimeError('Output verification failed: expected 48 kHz, '
                               '24-bit PCM with two channels.')
        if time_reference is not None and str(time_reference) != str(
                tag(output_info.get('format', {}).get('tags'), 'time_reference')):
            raise RuntimeError('Output verification failed: BWF timecode was not written.')
        if destination.exists():
            raise RuntimeError('Output already exists; refusing to overwrite.')
        partial.rename(destination)
    finally:
        if partial.exists():
            partial.unlink()
    return {'channels_mixed': len(active),
            'channels_skipped_silent': silent,
            'channels_polarity_flipped': flipped,
            'gain_db': round(gain, 2),
            'time_reference': time_reference,
            'warning': phase_warning(active, mix['rms'])}


def create_stereo_bin(pool, parent):
    taken = {f.GetName().lower() for f in items(parent.GetSubFolderList())}
    name, number = BIN_NAME, 1
    while name.lower() in taken:
        name = '{} {}'.format(BIN_NAME, number)
        number += 1
    folder = pool.AddSubFolder(parent, name)
    if not folder:
        raise RuntimeError('Could not create the {} bin.'.format(name))
    return folder


def path_key(path):
    return os.path.normcase(os.path.realpath(str(path)))


def import_files(pool, paths):
    """Import in one call; returns {path_key: MediaPoolItem}."""
    found = {}

    def collect(imported):
        for item in items(imported):
            found[path_key(item.GetClipProperty('File Path'))] = item

    # Resolve versions differ: some accept clip-info dictionaries,
    # while others require a plain list of file paths.
    collect(pool.ImportMedia([{'FilePath': p} for p in paths]))
    missing = [p for p in paths if path_key(p) not in found]
    if missing:
        collect(pool.ImportMedia(missing))
    return found


def write_log(directory, records):
    (directory / 'conversion_log.json').write_text(
        json.dumps(records, indent=2, ensure_ascii=False), encoding='utf-8')


def main(resolve_app=None):
    if resolve_app is None:
        import DaVinciResolveScript
        resolve_app = DaVinciResolveScript.scriptapp('Resolve')
    if not resolve_app:
        raise RuntimeError('Run this script inside an open DaVinci Resolve project.')
    project = resolve_app.GetProjectManager().GetCurrentProject()
    if not project:
        raise RuntimeError('Open a project first.')
    status = integrity()
    print('Selected Clips to Stereo v{} - (c) 2026 {}'.format(VERSION, AUTHOR))
    if status == 'official':
        print('Official version (unmodified).')
    elif status == 'modified':
        print('WARNING: this copy has been MODIFIED. It is not the official v{} '
              'from {}.'.format(VERSION, AUTHOR))
    else:
        print('Could not verify that this copy is the official version.')
    pool = project.GetMediaPool()
    selected = items(pool.GetSelectedClips())
    if not selected:
        raise RuntimeError('Select source clips in the Media Pool first.')
    ffmpeg = find_tool('ffmpeg', FFMPEG_PATH)
    ffprobe = find_tool('ffprobe', FFPROBE_PATH)
    original_folder = pool.GetCurrentFolder()
    run_id = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid4().hex[:8]
    directory = OUTPUT_ROOT / safe_name(project.GetName()) / run_id
    total = len(selected)

    # 1. Read everything needed from Resolve up front (main thread only).
    records, jobs = [], []
    for index, clip in enumerate(selected, 1):
        record = {'clip': clip.GetName(), 'status': 'failed',
                  'script_version': VERSION, 'integrity': status}
        records.append(record)
        try:
            reject_linked_audio(clip)
            source_text = clip.GetClipProperty('File Path')
            if not source_text:
                raise ValueError('No source file path (compound/multicam/timeline clips unsupported).')
            source = Path(source_text)
            if not source.is_file():
                raise ValueError('Source file is offline: {}'.format(source))
            record['source'] = str(source)
            start_timecode = (clip.GetClipProperty('Start TC') or
                              clip.GetClipProperty('Start Timecode'))
            record['start_timecode'] = start_timecode or ''
            destination = directory / '{:03d}_{}_Stereo.wav'.format(index, safe_name(source.stem))
            jobs.append((record, source, destination, start_timecode))
        except Exception as exc:
            record['error'] = str(exc)
            print('[{}/{}] FAILED {}: {}'.format(index, total, record['clip'], exc))
    if not jobs:
        print('Done: nothing converted; {} failed.'.format(total))
        return records

    # 2. Convert in parallel.
    directory.mkdir(parents=True, exist_ok=False)
    print('Mixing {} clip(s), {} at a time. Equal average of non-silent channels '
          'to both L and R.'.format(len(jobs), PARALLEL_JOBS))
    print('Output: {}'.format(directory))
    converted, done = [], 0
    with ThreadPoolExecutor(max_workers=PARALLEL_JOBS) as executor:
        futures = {executor.submit(convert, source, destination, ffmpeg, ffprobe,
                                   start_timecode): (record, destination, start_timecode)
                   for record, source, destination, start_timecode in jobs}
        for future in as_completed(futures):
            record, destination, start_timecode = futures[future]
            done += 1
            try:
                result = future.result()
                record.update((k, v) for k, v in result.items() if v is not None)
                record['output'] = str(destination)
                record['status'] = 'converted'
                converted.append((record, destination, start_timecode))
                notes = ['{} channel(s) mixed'.format(result['channels_mixed'])]
                if result['gain_db'] >= 0.05:
                    notes.append('+{:.1f} dB'.format(result['gain_db']))
                for key, label in (('channels_skipped_silent', 'silent skipped'),
                                   ('channels_polarity_flipped', 'polarity flipped')):
                    if result[key]:
                        notes.append('{}: {}'.format(label, ', '.join(map(str, result[key]))))
                print('[{}/{}] Converted {} ({})'.format(
                    done, len(jobs), destination.name, '; '.join(notes)))
                if result['warning']:
                    print('    WARNING: {}'.format(result['warning']))
            except Exception as exc:
                record['error'] = str(exc)
                print('[{}/{}] FAILED {}: {}'.format(done, len(jobs), record['clip'], exc))
            write_log(directory, records)

    # 3. Import everything into a new Stereo bin inside the current bin.
    bin_name = None
    if converted:
        try:
            parent = original_folder or pool.GetRootFolder()
            stereo_folder = create_stereo_bin(pool, parent)
            bin_name = stereo_folder.GetName()
            if not pool.SetCurrentFolder(stereo_folder):
                raise RuntimeError('Could not select the {} bin.'.format(bin_name))
            imported = import_files(pool, [str(d) for _, d, _ in converted])
            for record, destination, start_timecode in converted:
                item = imported.get(path_key(destination))
                if not item:
                    record['error'] = 'Resolve import failed. WAV is saved on disk for manual import.'
                    continue
                record['status'] = 'imported'
                if start_timecode:
                    set_property = getattr(item, 'SetClipProperty', None)
                    record['start_tc_set'] = bool(
                        callable(set_property) and set_property('Start TC', str(start_timecode)))
        except Exception as exc:
            for record, _, _ in converted:
                record.setdefault('error', '{} WAV is saved on disk.'.format(exc))
            print('Import FAILED: {}'.format(exc))
        finally:
            if original_folder:
                pool.SetCurrentFolder(original_folder)
        write_log(directory, records)

    success = sum(r['status'] == 'imported' for r in records)
    warnings = sum(bool(r.get('warning')) for r in records)
    print('Done: {} imported; {} failed{}.{}'.format(
        success, total - success,
        '; {} with phase warnings'.format(warnings) if warnings else '',
        ' Media Pool bin: {}'.format(bin_name) if bin_name else ''))
    print('Log: {}'.format(directory / 'conversion_log.json'))
    return records


if __name__ == '__main__':
    try:
        main(globals().get('resolve'))
    except Exception:
        traceback.print_exc()
