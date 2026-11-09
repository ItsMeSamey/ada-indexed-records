#!/usr/bin/env python3
"""Measure process memory using a completed benchmark's archives and fixed queries."""
import argparse
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from measure import Calls, equal_files, pin, validate_metrics

FIXTURES = ('tiny_repeated', 'mixed_repeated', 'mixed_entropy', 'cross_record')
MODES = ('grouped', 'per-record', 'monolithic', 'raw', 'zip', 'cache')
PHASES = ('exec_start', 'after_open', 'after_passes')
MAX_RECORD = 2_097_152
U64 = struct.Struct('<Q')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def executable(name):
    found = shutil.which(name)
    require(found is not None, 'required tool was not found: ' + name)
    return Path(found).resolve()


def checked_pin(path, expected):
    require(isinstance(expected, dict) and set(expected) == {'bytes', 'sha256'},
            'invalid file pin: ' + str(path))
    require(type(expected['bytes']) is int and expected['bytes'] >= 0,
            'invalid pinned byte count: ' + str(path))
    digest = expected['sha256']
    require(isinstance(digest, str) and len(digest) == 64
            and all(character in '0123456789abcdef' for character in digest),
            'invalid pinned digest: ' + str(path))
    require(pin(path) == expected, 'input file differs from accepted result: ' + str(path))


def accepted_measurements(directory):
    supplied = directory.resolve()
    supplied_result = supplied / 'RESULT.json'
    original = json.loads(supplied_result.read_text(encoding='utf-8'))
    require(isinstance(original, dict) and original.get('status') == 'PASS',
            'measurements must have a completed PASS result')
    controls = {'supplied-result': supplied_result}
    if 'fixtures' not in original:
        inner = supplied / 'measurements' / 'RESULT.json'
        checked_pin(inner, original.get('measurements'))
        controls['measurement-result'] = inner
        original = json.loads(inner.read_text(encoding='utf-8'))
        supplied = inner.parent
    require(isinstance(original, dict) and original.get('status') == 'PASS',
            'inner measurements must have a completed PASS result')
    cases = original.get('fixtures')
    require(isinstance(cases, list) and len(cases) == len(FIXTURES),
            'measurements must contain the four fixed fixtures')
    require(all(isinstance(case, dict) for case in cases), 'invalid fixture record')
    names = [case.get('name') for case in cases]
    require(all(isinstance(name, str) for name in names)
            and len(set(names)) == len(FIXTURES) and set(names) == set(FIXTURES),
            'fixture names are missing, duplicated or unsupported')
    require(all(isinstance(case.get('modes'), dict) and set(case['modes']) == set(MODES)
                for case in cases), 'accepted fixtures must each contain all six modes')
    return supplied, {case['name']: case for case in cases}, controls


def read_queries(path, record_count, declared_count):
    data = path.read_bytes()
    require(not data or data.endswith(b'\n'), 'query file must end each line with LF')
    queries = []
    for line in data[:-1].split(b'\n') if data else []:
        require(bool(line) and all(48 <= character <= 57 for character in line),
                'query file requires decimal ordinals')
        ordinal = int(line)
        require(ordinal < record_count, 'query ordinal is out of range')
        queries.append(ordinal)
    require(type(declared_count) is int and declared_count == len(queries),
            'query count differs from the accepted result')
    return queries


def expected_lengths(path, query_count):
    lengths = []
    with path.open('rb') as source:
        for pass_number in range(3):
            for position in range(query_count):
                header = source.read(8)
                require(len(header) == 8, 'truncated expected payload length')
                length = U64.unpack(header)[0]
                require(length <= MAX_RECORD, 'expected payload exceeds record limit')
                if pass_number == 0:
                    lengths.append(length)
                else:
                    require(length == lengths[position], 'expected pass lengths differ')
                remaining = length
                while remaining:
                    block = source.read(min(remaining, 1024 * 1024))
                    require(bool(block), 'truncated expected payload')
                    remaining -= len(block)
        require(source.read(1) == b'', 'unexpected bytes after expected payloads')
    return lengths


def bind_fixture(directory, case, controls):
    name = case['name']
    folder = directory / name
    require(type(case.get('record_count')) is int and case['record_count'] == 65_536,
            'fixture record count differs from the fixed comparison')
    require(isinstance(case.get('modes'), dict) and set(case['modes']) == set(MODES),
            'fixture must contain all six accepted modes')
    files = {'input.records': case.get('input'), 'raw.offsets': case.get('offsets'),
             'queries.txt': case.get('query_file'),
             'expected.payloads': case.get('expected_payloads')}
    for mode in MODES:
        record = case['modes'][mode]
        require(isinstance(record, dict) and record.get('exact_all_records_and_queries') is True,
                'mode lacks accepted exact-byte validation: ' + name + '/' + mode)
        for operation in ('pack_native', 'bench_native'):
            native = record.get(operation)
            require(isinstance(native, dict) and type(native.get('exit_code')) is int
                    and native['exit_code'] == 0 and native.get('failure') is None,
                    'mode has no successful native receipt: ' + name + '/' + mode)
        filename = (mode + '.zst' if mode in MODES[:3] else
                    'input.records' if mode == 'raw' else
                    'records.zip' if mode == 'zip' else 'monolithic.zst')
        checked_pin(folder / filename, record.get('archive'))
        if filename in files:
            require(files[filename] == record['archive'], 'conflicting archive pins')
        files[filename] = record['archive']
    for filename, expected in files.items():
        path = folder / filename
        checked_pin(path, expected)
        controls['measurements/' + name + '/' + filename] = path
    queries = read_queries(folder / 'queries.txt', case['record_count'], case.get('query_count'))
    lengths = expected_lengths(folder / 'expected.payloads', len(queries))
    return folder, queries, lengths


def memory_fields(metric, mode, pid):
    if mode in MODES[:3]:
        require(metric.get('measurement_kind') == 'memory_only_successor'
                and metric.get('timing_role') == 'incidental', 'invalid Ada memory report kind')
        require(metric.get('pid') == pid and metric.get('memory_source') == '/proc/self/status',
                'Ada memory report process mismatch')
        require(set(metric['memory_snapshots']) == set(PHASES), 'invalid Ada memory phases')
        values = [metric['memory_snapshots'][phase] for phase in PHASES]
        snapshots = [{'phase': phase, 'vm_hwm_bytes': value['vm_hwm_bytes'],
                      'vm_rss_bytes': value['vm_rss_bytes']}
                     for phase, value in zip(PHASES, values)]
    else:
        require(metric.get('status') == 'PASS' and metric.get('mode') == mode,
                'invalid baseline memory report')
        values = metric['memory']['snapshots']
        require([value['phase'] for value in values]
                == ['own_exec_start', 'after_open', 'after_three_query_passes'],
                'invalid baseline memory phases')
        require(all(value['pid'] == pid and value['source'] == '/proc/self/status'
                    for value in values), 'baseline memory report process mismatch')
        snapshots = [{'phase': phase, 'vm_hwm_bytes': value['VmHWM_bytes'],
                      'vm_rss_bytes': value['VmRSS_bytes']}
                     for phase, value in zip(PHASES, values)]
    previous = 0
    for row in snapshots:
        require(all(type(row[key]) is int and row[key] >= 0
                    for key in ('vm_hwm_bytes', 'vm_rss_bytes')), 'invalid memory values')
        require(row['vm_hwm_bytes'] >= previous and row['vm_hwm_bytes'] >= row['vm_rss_bytes'],
                'inconsistent memory highwater')
        previous = row['vm_hwm_bytes']
    return snapshots


def main():
    require(__debug__, 'run without Python optimization flags')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--measurements', required=True, type=Path,
                        help='completed benchmark output or its measurements subdirectory')
    parser.add_argument('--output', required=True, type=Path,
                        help='new directory for build logs, payload checks and memory reports')
    parser.add_argument('--fixture', choices=FIXTURES,
                        help='one retained fixture for a portability check; default: all four')
    parser.add_argument('--gprbuild', default='gprbuild')
    parser.add_argument('--zstd', default='zstd')
    parser.add_argument('--config', type=Path, help='optional existing GPR configuration file')
    args = parser.parse_args()
    selected = (args.fixture,) if args.fixture is not None else FIXTURES
    require(not os.path.lexists(args.output), 'output already exists')
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True)
    (output / '.tmp').mkdir(mode=0o700)
    calls = Calls(output)
    rows = []
    result = {'status': 'FAIL', 'scope': 'Process memory over existing accepted archives and fixed queries',
              'memory_scope': 'Approximate own-exec worker VmHWM/VmRSS; decoder child is separate',
              'timing_role': 'incidental', 'selected_fixtures': list(selected),
              'selection_role': ('portability_check' if args.fixture is not None
                                 else 'complete_memory_comparison'),
              'expected_memory_calls': len(selected) * len(MODES), 'rows': rows}
    try:
        gprbuild = executable(args.gprbuild)
        zstd = executable(args.zstd)
        directory, cases, controls = accepted_measurements(args.measurements)
        source_files = ['scripts/memory.py', 'tests/measure.py', 'tests/fixtures.py',
                        'tests/baseline_memory_worker.py', 'docs/FORMAT.md',
                        'records_bench_memory.gpr', 'indexed_records.gpr',
                        'src/records_bench_memory.adb', 'src/indexed_records.ads',
                        'src/indexed_records.adb', 'src/zstd_codec.ads', 'src/zstd_codec.adb']
        controls.update({'release/' + name: ROOT / name for name in source_files})
        controls.update({'tools/gprbuild': gprbuild, 'tools/zstd': zstd})
        if args.config is not None:
            controls['supplied-gpr-config'] = args.config.resolve()
        bound = {name: bind_fixture(directory, cases[name], controls) for name in selected}
        before = {label: pin(path) for label, path in controls.items()}
        result['source_before'] = before
        (output / 'SOURCE_BEFORE.json').write_text(json.dumps(before, indent=2) + '\n')
        build = [gprbuild, '-p', '-P', ROOT / 'records_bench_memory.gpr']
        if args.config is not None:
            build.append('--config=' + str(args.config.resolve()))
        result['build_native'] = calls.run('memory-driver build', build)
        require({label: pin(path) for label, path in controls.items()} == before,
                'controlled source or input changed during build')
        ada = ROOT / 'bin' / 'records_bench_memory'
        binary = pin(ada)
        result['memory_driver'] = binary
        worker = ROOT / 'tests' / 'baseline_memory_worker.py'
        for name in selected:
            folder, queries, lengths = bound[name]
            destination = output / name
            destination.mkdir()
            for mode in MODES:
                archive = folder / (mode + '.zst' if mode in MODES[:3] else
                                    'input.records' if mode == 'raw' else
                                    'records.zip' if mode == 'zip' else 'monolithic.zst')
                report = destination / (mode + '.json')
                if mode in MODES[:3]:
                    command = [ada, archive, folder / 'queries.txt', report]
                else:
                    command = [sys.executable, '-I', '-B', worker, '--zstd', zstd,
                               'bench', mode, archive, folder / 'queries.txt', report]
                    if mode == 'raw':
                        command.append(folder / 'raw.offsets')
                native = calls.run(name + ' ' + mode + ' memory', command)
                payloads = Path(str(report) + '.payloads')
                equal_files(folder / 'expected.payloads', payloads)
                metric = json.loads(report.read_text(encoding='utf-8'))
                validate_metrics(mode, metric, queries, lengths, cases[name]['record_count'])
                snapshots = memory_fields(metric, mode, native['pid'])
                rows.append({'fixture': name, 'mode': mode, 'pid': native['pid'],
                             'snapshots': snapshots, 'report': pin(report), 'payloads': pin(payloads),
                             'cache_child': metric.get('memory', {}).get('cache_child'),
                             'native_reported_rusage_peak_bytes': native['peak_rss_bytes']})
                (output / 'PARTIAL.json').write_text(json.dumps(rows, indent=2) + '\n')
        after = {label: pin(path) for label, path in controls.items()}
        require(after == before, 'controlled source or input changed during measurement')
        require(pin(ada) == binary, 'memory driver changed during measurement')
        expected_calls = len(selected) * len(MODES)
        require(len(rows) == expected_calls and len(calls.rows) == expected_calls + 1,
                'incomplete selected memory comparison')
        result.update(status='PASS', source_after=after, memory_call_count=len(rows),
                      complete_comparison=(selected == FIXTURES))
    except BaseException as error:
        result['error'] = type(error).__name__ + ': ' + str(error)
        (output / 'FAILURE.txt').write_text(traceback.format_exc())
    result['call_count'] = len(calls.rows)
    with (output / 'RESULT.json').open('x', encoding='utf-8') as report:
        json.dump(result, report, indent=2, sort_keys=True)
        report.write('\n')
    print(json.dumps({'status': result['status'], 'memory_rows': len(rows),
                      'error': result.get('error')}))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print('memory: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
