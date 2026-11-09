#!/usr/bin/env python3
"""Build the CLI and pinned reference reader, then run the correctness tests."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def pin(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return {'bytes': path.stat().st_size, 'sha256': digest.hexdigest()}


def executable(name):
    found = shutil.which(name)
    if found is None:
        raise ValueError('required tool was not found: ' + name)
    return str(Path(found).resolve())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path,
                        help='fresh directory for the reference build, logs and test results')
    parser.add_argument('--gprbuild', default='gprbuild')
    parser.add_argument('--cc', default='gcc')
    parser.add_argument('--zstd', default='zstd')
    parser.add_argument('--config', type=Path,
                        help='optional existing GPR configuration file')
    parser.add_argument('--reference-wrapper', type=Path,
                        default=ROOT / 'tests' / 'seekable_reference.c')
    parser.add_argument('--reference-root', type=Path,
                        default=ROOT / 'vendor' / 'zstd')
    args = parser.parse_args()
    output = args.output.resolve()
    if os.path.lexists(output):
        raise ValueError('output already exists: ' + str(output))
    output.mkdir(mode=0o700, parents=True)
    temporary = output / 'tmp'
    temporary.mkdir(mode=0o700)
    environment = os.environ.copy()
    environment['TMPDIR'] = str(temporary)
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    result = {'status': 'FAIL', 'commands': [],
              'scope': 'release-source build and frozen small-fixture correctness; no benchmark',
              'build_layout': 'normal GPR obj/lib/bin paths inside the release tree',
              'output': str(output)}

    def run(label, command):
        number = len(result['commands'])
        stdout = output / f'{number:02d}-{label}.stdout'
        stderr = output / f'{number:02d}-{label}.stderr'
        started = time.monotonic()
        with stdout.open('xb') as out, stderr.open('xb') as err:
            completed = subprocess.run(command, cwd=ROOT, env=environment,
                                       stdout=out, stderr=err)
        row = {'label': label, 'argv': command, 'cwd': str(ROOT),
               'exit_code': completed.returncode,
               'wall_seconds': time.monotonic() - started,
               'stdout': pin(stdout), 'stderr': pin(stderr)}
        result['commands'].append(row)
        (output / 'COMMANDS.json').write_text(json.dumps(result['commands'], indent=2) + '\n')
        if completed.returncode != 0:
            raise RuntimeError(label + ' failed; see its retained stdout and stderr')

    try:
        gprbuild = executable(args.gprbuild)
        cc = executable(args.cc)
        zstd = executable(args.zstd)
        wrapper = args.reference_wrapper.resolve()
        vendor = args.reference_root.resolve()
        seekable = vendor / 'contrib' / 'seekable_format'
        common = vendor / 'lib' / 'common'
        reference = output / 'reference_cli'
        candidate = ROOT / 'bin' / 'records_cli'
        controlled = [
            ROOT / 'indexed_records.gpr', ROOT / 'records_cli.gpr',
            *(ROOT / 'src' / name for name in
              ('indexed_records.ads', 'indexed_records.adb', 'zstd_codec.ads',
               'zstd_codec.adb', 'records_cli.adb')),
            ROOT / 'docs' / 'FORMAT.md', Path(__file__).resolve(),
            *(ROOT / 'tests' / name for name in
              ('fixtures.py', 'container_oracle.py', 'qualify.py')),
            wrapper, seekable / 'zstdseek_decompress.c', seekable / 'zstd_seekable.h',
            common / 'xxhash.c', common / 'xxhash.h',
        ]
        before = {str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path): pin(path)
                  for path in controlled}
        result['source_before'] = before
        build = [gprbuild, '-p', '-P', str(ROOT / 'records_cli.gpr')]
        if args.config is not None:
            build.append('--config=' + str(args.config.resolve()))
        run('gprbuild', build)
        if not candidate.is_file():
            raise ValueError('GPRbuild did not produce bin/records_cli')
        run('reference-build', [cc, '-O2', '-Wall', '-Wextra', '-Werror',
                               '-I' + str(seekable), '-I' + str(common),
                               str(wrapper), str(seekable / 'zstdseek_decompress.c'),
                               str(common / 'xxhash.c'), '-lzstd', '-o', str(reference)])
        run('correctness', [sys.executable, '-I', '-B', str(ROOT / 'tests' / 'qualify.py'),
                            '--candidate', str(candidate), '--reference', str(reference),
                            '--zstd', zstd, '--output', str(output / 'checks')])
        qualification_path = output / 'checks' / 'RESULT.json'
        qualification = json.loads(qualification_path.read_text())
        if (qualification.get('status') != 'PASS'
                or qualification.get('format_and_sources_unchanged') is not True):
            raise ValueError('correctness result is incomplete or failed')
        after = {str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path): pin(path)
                 for path in controlled}
        if before != after:
            raise ValueError('controlled release source changed during validation')
        result.update(status='PASS', source_after=after,
                      candidate=pin(candidate), reference=pin(reference),
                      qualification=pin(qualification_path),
                      correctness_call_count=qualification['call_count'])
    except Exception as error:
        result['error'] = type(error).__name__ + ': ' + str(error)
    with (output / 'RESULT.json').open('x') as report:
        json.dump(result, report, indent=2, sort_keys=True)
        report.write('\n')
    print(json.dumps({'status': result['status'], 'output': str(output),
                      'error': result.get('error')}))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print('test: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
