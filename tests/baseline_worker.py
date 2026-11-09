"""Raw, ZIP-per-record and full-cache cost baselines for frozen synthetic records."""
import argparse
from array import array
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import resource
import statistics
import struct
import subprocess
import sys
import time
import zipfile

MAX_RECORD = 2_097_152
U64 = struct.Struct('<Q')


def fresh(path):
    if os.path.lexists(path):
        raise ValueError(f'output already exists: {path}')


def exact_read(stream, length):
    parts = bytearray()
    while len(parts) < length:
        part = stream.read(length - len(parts))
        if not part:
            raise ValueError('truncated canonical stream')
        parts.extend(part)
    return bytes(parts)


def write_all(stream, data):
    remaining = memoryview(data)
    while remaining:
        written = stream.write(remaining)
        if written is None or written <= 0:
            raise OSError('canonical payload write made no progress')
        remaining = remaining[written:]


def canonical_records(stream):
    offset = 0
    while True:
        first = stream.read(1)
        if not first:
            return
        header = first + exact_read(stream, 7)
        length = U64.unpack(header)[0]
        if length > MAX_RECORD:
            raise ValueError('record exceeds the 2097152-byte limit')
        yield offset, exact_read(stream, length)
        offset += 8 + length


def build(mode, canonical, destination):
    fresh(destination)
    wall = time.perf_counter_ns()
    cpu = time.process_time_ns()
    count = 0
    with canonical.open('rb') as source:
        if mode == 'raw-build':
            with destination.open('xb') as output:
                for offset, _payload in canonical_records(source):
                    output.write(U64.pack(offset))
                    count += 1
        else:
            with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=3, allowZip64=True) as output:
                output.comment = b''
                for _offset, payload in canonical_records(source):
                    if count >= 100_000_000:
                        raise ValueError('record count exceeds the eight-digit ZIP name range')
                    info = zipfile.ZipInfo(f'{count:08d}', (1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = 0o100644 << 16
                    info.extra = b''
                    info.comment = b''
                    output.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED,
                                    compresslevel=3)
                    count += 1
    result = {'status': 'PASS', 'command': mode, 'records': count,
              'build_wall_ns': time.perf_counter_ns() - wall,
              'build_cpu_ns': time.process_time_ns() - cpu,
              'canonical_bytes': canonical.stat().st_size,
              'output_bytes': destination.stat().st_size,
              'timer_scope': 'open, canonical parsing, output construction and close'}
    if mode == 'raw-build':
        result['archive_bytes_including_offsets'] = result['canonical_bytes'] + result['output_bytes']
        result['offset_encoding'] = 'little-endian uint64 canonical record-header positions'
    else:
        result['zip_contract'] = 'one eight-digit ordinal entry per record; DEFLATE level 3; fixed 1980 timestamp'
        result['comparison_scope'] = 'ZIP format/Python runtime baseline; not measured Zipada or isolated codec effect'
    print(json.dumps(result, sort_keys=True))


def query_file(path):
    data = path.read_bytes()
    if not data:
        return []
    if not data.endswith(b'\n'):
        raise ValueError('query file must end each decimal ordinal with LF')
    result = []
    for line in data[:-1].split(b'\n'):
        if not line or any(byte < 48 or byte > 57 for byte in line):
            raise ValueError('query file requires one unsigned decimal ordinal per LF line')
        value = int(line)
        if value >= 1 << 64:
            raise ValueError('query ordinal exceeds uint64')
        result.append(value)
    return result


def child_cpu_ns():
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return round((usage.ru_utime + usage.ru_stime) * 1_000_000_000)


def latency_summary(samples):
    ordered = sorted(samples)
    if not ordered:
        return {'count': 0, 'sum_ns': 0, 'min_ns': None, 'median_ns': None,
                'p95_ns': None, 'p99_ns': None, 'max_ns': None}
    return {'count': len(samples), 'sum_ns': sum(samples), 'min_ns': ordered[0],
            'median_ns': statistics.median(ordered),
            'p95_ns': ordered[math.ceil(0.95 * len(ordered)) - 1],
            'p99_ns': ordered[math.ceil(0.99 * len(ordered)) - 1],
            'max_ns': ordered[-1]}


def load_offsets(path, canonical_size):
    encoded = path.read_bytes()
    if len(encoded) % 8:
        raise ValueError('offset file length is not a multiple of eight')
    offsets = array('Q')
    if offsets.itemsize != 8:
        raise ValueError('runtime unsigned long long size is not eight bytes')
    offsets.frombytes(encoded)
    if sys.byteorder != 'little':
        offsets.byteswap()
    previous = -1
    for offset in offsets:
        if offset <= previous or offset + 8 > canonical_size:
            raise ValueError('invalid raw offset')
        previous = offset
    if (offsets and offsets[0] != 0) or (not offsets and canonical_size != 0):
        raise ValueError('raw offset count or first position differs from canonical input')
    return offsets


def cache_offsets(canonical):
    offsets = array('Q')
    if offsets.itemsize != 8:
        raise ValueError('runtime unsigned long long size is not eight bytes')
    position = 0
    while position < len(canonical):
        if len(canonical) - position < 8:
            raise ValueError('truncated cached canonical length')
        length = U64.unpack_from(canonical, position)[0]
        if length > MAX_RECORD:
            raise ValueError('cached record exceeds the 2097152-byte limit')
        end = position + 8 + length
        if end > len(canonical):
            raise ValueError('truncated cached canonical payload')
        offsets.append(position)
        position = end
    return offsets


def run_bench(mode, archive, queries, offsets_path, payload_file, stack, result):
    result.update({'status': 'PASS', 'mode': mode, 'passes': [], 'query_count': len(queries),
              'cache_policy': 'one open per worker; three identical query passes; no OS cache reset',
              'query_timer_scope': 'lookup through returned payload; payload serialization/write follows timer',
              'pass_timer_scope': 'whole query loop including canonical payload writes',
              'open_timer_scope': 'archive/index open and parsing; cache additionally includes zstd child, pipe and canonical parsing'})
    child_start = child_cpu_ns()
    cpu_start = time.process_time_ns()
    wall_start = time.perf_counter_ns()
    archive_bytes = archive.stat().st_size
    result['archive_file_bytes'] = archive_bytes
    if mode == 'raw':
        offsets = load_offsets(offsets_path, archive_bytes)
        source = stack.enter_context(archive.open('rb'))
        result['archive_bytes'] = archive_bytes + offsets_path.stat().st_size
        result['index_bytes'] = offsets_path.stat().st_size
        result['open_application_bytes_read'] = result['index_bytes']
        result['resident_offsets_bytes'] = len(offsets) * offsets.itemsize
        count = len(offsets)

        def read_record(ordinal):
            source.seek(offsets[ordinal])
            length = U64.unpack(exact_read(source, 8))[0]
            if length > MAX_RECORD:
                raise ValueError('raw record exceeds the 2097152-byte limit')
            return exact_read(source, length)

        def byte_estimate(ordinal, payload):
            return 8 + len(payload), 0

        result['byte_accounting'] = 'query bytes read are application-returned length+payload bytes, not kernel I/O; no decompression'
    elif mode == 'zip':
        source = stack.enter_context(zipfile.ZipFile(archive, 'r'))
        entries = source.infolist()
        for ordinal, entry in enumerate(entries):
            if entry.filename != f'{ordinal:08d}' or entry.is_dir():
                raise ValueError('ZIP entries are not contiguous eight-digit ordinals')
            if entry.file_size > MAX_RECORD or entry.compress_type != zipfile.ZIP_DEFLATED:
                raise ValueError('ZIP record size or compression differs from baseline')
        count = len(entries)
        result['archive_bytes'] = archive_bytes
        result['index_bytes'] = None
        result['open_application_bytes_read'] = None
        result['metadata_note'] = 'ZIP headers/central directory are included in archive bytes; Python metadata memory and open I/O are not isolated'
        result['comparison_scope'] = 'ZIP format/Python runtime baseline; not measured Zipada or isolated codec effect'

        def read_record(ordinal):
            return source.read(entries[ordinal])

        def byte_estimate(ordinal, payload):
            return entries[ordinal].compress_size, len(payload)

        result['byte_accounting'] = 'query compressed-payload estimate excludes local headers, metadata, buffering and kernel I/O; decoded bytes equal returned payload lengths'
    else:
        command = ['/usr/bin/zstd', '-q', '-d', '-c', str(archive)]
        decoded = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        result['decode_command'] = command
        result['decode_exit_code'] = decoded.returncode
        result['decode_stderr'] = decoded.stderr.decode('utf-8', errors='replace')
        if decoded.returncode:
            raise ValueError('zstd cache decode failed: ' + result['decode_stderr'].strip())
        canonical = decoded.stdout
        offsets = cache_offsets(canonical)
        count = len(offsets)
        result['archive_bytes'] = archive_bytes
        result['index_bytes'] = None
        result['cache_canonical_bytes'] = len(canonical)
        result['cache_offsets_bytes'] = len(offsets) * offsets.itemsize
        result['cache_resident_payload_and_offsets_bytes'] = len(canonical) + len(offsets) * offsets.itemsize
        result['cache_python_object_size_estimate_bytes'] = sys.getsizeof(canonical) + sys.getsizeof(offsets)
        result['cache_memory_note'] = 'canonical+offset bytes are logical resident storage; object estimate is partial, not total Python/allocator/transient memory; worker process-lifetime RSS is captured externally and may include pre-exec memory'
        result['open_input_archive_bytes_estimate'] = archive_bytes
        result['open_decoded_bytes'] = len(canonical)

        def read_record(ordinal):
            start = offsets[ordinal]
            length = U64.unpack_from(canonical, start)[0]
            return canonical[start + 8:start + 8 + length]

        def byte_estimate(ordinal, payload):
            return 0, 0

        result['byte_accounting'] = 'full archive size estimates open input; canonical bytes decoded once at open; query copies read cached memory and perform no file read/decompression'
    result['open_wall_ns'] = time.perf_counter_ns() - wall_start
    result['open_own_cpu_ns'] = time.process_time_ns() - cpu_start
    result['open_child_cpu_ns'] = child_cpu_ns() - child_start
    result['open_cpu_ns'] = result['open_own_cpu_ns'] + result['open_child_cpu_ns']
    result['record_count'] = count
    if any(ordinal >= count for ordinal in queries):
        raise ValueError('query ordinal outside archive')
    for pass_number in range(1, 4):
        latencies = []
        read_bytes = decoded_bytes = payload_bytes = 0
        wall = time.perf_counter_ns()
        cpu = time.process_time_ns()
        for ordinal in queries:
            start = time.perf_counter_ns()
            payload = read_record(ordinal)
            latencies.append(time.perf_counter_ns() - start)
            read_count, decoded_count = byte_estimate(ordinal, payload)
            read_bytes += read_count
            decoded_bytes += decoded_count
            payload_bytes += len(payload)
            write_all(payload_file, U64.pack(len(payload)))
            write_all(payload_file, payload)
        pass_wall = time.perf_counter_ns() - wall
        pass_cpu = time.process_time_ns() - cpu
        result['passes'].append({'pass': pass_number, 'wall_ns': pass_wall, 'cpu_ns': pass_cpu,
                                 'query_latencies_ns': latencies,
                                 'query_latency': latency_summary(latencies),
                                 'query_time_sum_ns': sum(latencies),
                                 'bytes_read_or_compressed_payload_estimate': read_bytes,
                                 'bytes_decoded': decoded_bytes,
                                 'payload_bytes': payload_bytes,
                                 'canonical_output_bytes': payload_bytes + 8 * len(queries)})
    return result


def bench(mode, archive, query_path, output, offsets):
    payload_path = Path(str(output) + '.payloads')
    fresh(output)
    fresh(payload_path)
    queries = query_file(query_path)
    failure = None
    result = {'status': 'FAIL', 'mode': mode}
    with output.open('x', encoding='utf-8') as report:
        try:
            with ExitStack() as stack:
                payload_file = stack.enter_context(payload_path.open('xb', buffering=0))
                run_bench(mode, archive, queries, offsets, payload_file, stack, result)
            result['payloads_path'] = str(payload_path)
            result['payloads_encoding'] = 'three passes in query order; repeated LE64 length then exact payload bytes'
            result['payloads_bytes'] = payload_path.stat().st_size
        except Exception as error:
            failure = error
            result['status'] = 'FAIL'
            result['error'] = type(error).__name__ + ': ' + str(error)
        json.dump(result, report, indent=2, sort_keys=True)
        report.write('\n')
    if failure is not None:
        raise failure
    print(json.dumps({'status': 'PASS', 'mode': mode, 'records': result['record_count'],
                      'queries': len(queries), 'passes': 3, 'output': str(output)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('raw-build', 'zip-build'):
        command = commands.add_parser(name)
        command.add_argument('canonical', type=Path)
        command.add_argument('destination', type=Path)
    command = commands.add_parser('bench')
    command.add_argument('mode', choices=('raw', 'zip', 'cache'))
    command.add_argument('archive', type=Path)
    command.add_argument('queries', type=Path)
    command.add_argument('output', type=Path)
    command.add_argument('offsets', nargs='?', type=Path)
    args = parser.parse_args()
    if args.command == 'bench':
        if (args.mode == 'raw') != (args.offsets is not None):
            parser.error('OFFSETS is required only for raw mode')
        bench(args.mode, args.archive, args.queries, args.output, args.offsets)
    else:
        build(args.command, args.canonical, args.destination)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('baseline_worker: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
