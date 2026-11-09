#!/usr/bin/env python3
"""Build the CLI and query driver, then run the fixed synthetic comparison."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test import ROOT, executable, pin


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path,
                        help='fresh directory for logs, generated archives and measurements')
    parser.add_argument('--gprbuild', default='gprbuild')
    parser.add_argument('--config', type=Path,
                        help='optional existing GPR configuration file')
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
              'scope': 'fixed synthetic comparison over all released cost fixtures',
              'memory_scope': 'wait4 process-lifetime peaks may include pre-exec memory',
              'output': str(output)}

    def run(label, command):
        number = len(result['commands'])
        stdout = output / f'{number:02d}-{label}.stdout'
        stderr = output / f'{number:02d}-{label}.stderr'
        started = time.monotonic()
        with stdout.open('xb') as out, stderr.open('xb') as err:
            completed = subprocess.run(command, cwd=ROOT, env=environment,
                                       stdout=out, stderr=err)
        result['commands'].append({'label': label, 'argv': command,
                                   'exit_code': completed.returncode,
                                   'wall_seconds': time.monotonic() - started,
                                   'stdout': pin(stdout), 'stderr': pin(stderr)})
        (output / 'COMMANDS.json').write_text(json.dumps(result['commands'], indent=2) + '\n')
        if completed.returncode != 0:
            raise RuntimeError(label + ' failed; see retained command output')

    try:
        gprbuild = executable(args.gprbuild)
        controlled = [
            Path(__file__).resolve(), ROOT / 'scripts' / 'test.py', ROOT / 'docs' / 'FORMAT.md',
            *(ROOT / name for name in ('indexed_records.gpr', 'records_cli.gpr', 'records_bench.gpr')),
            *(ROOT / 'src' / name for name in
              ('indexed_records.ads', 'indexed_records.adb', 'zstd_codec.ads',
               'zstd_codec.adb', 'records_cli.adb', 'records_bench.adb')),
            *(ROOT / 'tests' / name for name in ('fixtures.py', 'measure.py', 'baseline_worker.py')),
        ]
        before = {str(path.relative_to(ROOT)): pin(path) for path in controlled}
        result['source_before'] = before
        for project in ('records_cli', 'records_bench'):
            command = [gprbuild, '-p', '-P', str(ROOT / (project + '.gpr'))]
            if args.config is not None:
                command.append('--config=' + str(args.config.resolve()))
            run('build-' + project, command)
        run('measure', [sys.executable, '-I', '-B', str(ROOT / 'tests' / 'measure.py'),
                        '--candidate', str(ROOT / 'bin' / 'records_cli'),
                        '--bench', str(ROOT / 'bin' / 'records_bench'),
                        '--baseline', str(ROOT / 'tests' / 'baseline_worker.py'),
                        '--output', str(output / 'measurements')])
        measured_path = output / 'measurements' / 'RESULT.json'
        measured = json.loads(measured_path.read_text())
        if measured.get('status') != 'PASS' or len(measured.get('fixtures', [])) != 4:
            raise ValueError('matched comparison is incomplete or failed')
        after = {str(path.relative_to(ROOT)): pin(path) for path in controlled}
        if before != after:
            raise ValueError('controlled release source changed during measurement')
        result.update(status='PASS', source_after=after, measurements=pin(measured_path))
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
        print('benchmark: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
