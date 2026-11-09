#!/usr/bin/env python3
"""Independent small-fixture qualification."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import struct
import subprocess
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures as frozen
import container_oracle as container


def need(condition, message):
    if not condition:
        raise AssertionError(message)


def pin(path):
    data = path.read_bytes()
    return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


class Driver:
    def __init__(self, args):
        self.args = args
        self.calls = []
        self.cases = []
        self.output = args.output

    def call(self, label, command, input_bytes=None, reject=False):
        number = len(self.calls)
        stem = self.output / ('call-%04d' % number)
        start = time.monotonic()
        done = subprocess.run(list(map(str, command)), input=input_bytes, capture_output=True)
        stdout = stem.with_suffix('.stdout')
        stderr = stem.with_suffix('.stderr')
        stdout.write_bytes(done.stdout)
        stderr.write_bytes(done.stderr)
        receipt = {'label': label, 'command': list(map(str, command)),
                   'exit_code': done.returncode, 'wall_seconds': time.monotonic() - start,
                   'stdout': pin(stdout), 'stderr': pin(stderr), 'expected_rejection': reject}
        self.calls.append(receipt)
        (self.output / 'CALLS.json').write_text(json.dumps(self.calls, indent=2))
        need(done.returncode != 0 if reject else done.returncode == 0, label + ': unexpected exit')
        return done.stdout

    def candidate(self, label, *arguments, reject=False):
        return self.call(label, [self.args.candidate, *arguments], reject=reject)

    def decode(self, label, frame):
        return self.call(label, [self.args.zstd, '-q', '-d', '-c'], bytes(frame))

    def compress_index(self, label, plain):
        source = self.output / (label + '.index-plain')
        source.write_bytes(plain)
        return self.call(label, [self.args.zstd, '-q', '-3', '--check', '-c', source])

    def reference(self, label, *arguments):
        return self.call(label, [self.args.reference, *arguments])

    def single_frame(self, label, frame, expected_size):
        source = self.output / (label + '.frame')
        source.write_bytes(bytes(frame))
        observed = json.loads(self.reference(label, 'frame-info', source))
        need(observed['compressed_size'] == len(frame), label + ': exact single-frame extent')
        need(observed['content_size'] == expected_size, label + ': exact frame content size')

    def valid(self, name, items, mode):
        prefix = name + '-' + mode
        canonical = b''.join(frozen.canonical_chunks(items))
        source = self.output / (prefix + '.records')
        archive = self.output / (prefix + '.zst')
        source.write_bytes(canonical)
        self.candidate(prefix + ' pack', 'pack', mode, source, archive)
        blob = archive.read_bytes()
        layout = container.parse_layout(blob)
        if mode == 'grouped':
            groups = list(frozen.expected_groups(items))
        elif mode == 'per-record':
            groups = [[item] for item in items] or [[]]
        else:
            groups = [items]
        counts = [len(group) for group in groups]
        expected_frames = [b''.join(frozen.canonical_chunks(group)) for group in groups]
        need(len(layout['data_frames']) == len(groups), prefix + ': frame grouping')
        self.single_frame(prefix + ' index frame-info', layout['index_frame']['inner_bytes'],
                          len(frozen.index_plain(counts)))
        decoded_index = self.decode(prefix + ' independent index decode', layout['index_frame']['inner_bytes'])
        need(decoded_index == frozen.index_plain(counts), prefix + ': ordinal index bytes')
        need(self.decode(prefix + ' ordinary zstd decode', blob) == canonical, prefix + ': ordinary decode')
        for i, (frame, expected) in enumerate(zip(layout['data_frames'], expected_frames)):
            self.single_frame(prefix + ' data frame-info ' + str(i), frame['bytes'], len(expected))
            need(frame['decompressed_size'] == len(expected), prefix + ': declared frame size')
            need(self.decode(prefix + ' independent data frame ' + str(i), frame['bytes']) == expected,
                 prefix + ': data frame bytes')
        info = json.loads(self.candidate(prefix + ' info', 'info', archive))
        need(info['count'] == len(items) and info['data_frames'] == len(groups), prefix + ': info counts')
        need(info['archive_bytes'] == len(blob), prefix + ': archive size')
        need(info['index_bytes'] == layout['index_frame']['compressed_size'] + len(layout['table']),
             prefix + ': complete index accounting')
        entries = layout['data_frames'] + [layout['index_frame']]
        need(info['compressed_sizes'] == [frame['compressed_size'] for frame in entries], prefix + ': compressed sizes')
        need(info['decompressed_sizes'] == [frame['decompressed_size'] for frame in entries], prefix + ': decoded sizes')
        self.candidate(prefix + ' verify', 'verify', archive)
        unpacked = self.output / (prefix + '.unpacked')
        self.candidate(prefix + ' unpack', 'unpack', archive, unpacked)
        need(unpacked.read_bytes() == canonical, prefix + ': exact canonical round trip')
        need(frozen.unpack_canonical(unpacked.read_bytes()) == items, prefix + ': exact record round trip')
        for ordinal, expected in enumerate(items):
            target = self.output / (prefix + '.get-' + str(ordinal))
            self.candidate(prefix + ' get ' + str(ordinal), 'get', archive, ordinal, target)
            need(target.read_bytes() == expected, prefix + ': ordinal bytes')
        self.candidate(prefix + ' out of range', 'get', archive, len(items), self.output / (prefix + '.invalid'), reject=True)
        ref = json.loads(self.reference(prefix + ' reference info', 'info', archive))
        need(ref['frame_count'] == len(entries), prefix + ': reference frame count')
        need(len(ref['frames']) == len(entries), prefix + ': reference frame rows')
        need([frame['compressed_offset'] for frame in ref['frames']] == [frame['offset'] for frame in entries], prefix + ': reference compressed offsets')
        need([frame['compressed_size'] for frame in ref['frames']] == [frame['compressed_size'] for frame in entries], prefix + ': reference compressed sizes')
        need([frame['decompressed_size'] for frame in ref['frames']] == [frame['decompressed_size'] for frame in entries], prefix + ': reference decoded sizes')
        decoded_offsets = []
        total = 0
        for frame in entries:
            decoded_offsets.append(total)
            total += frame['decompressed_size']
        need([frame['decompressed_offset'] for frame in ref['frames']] == decoded_offsets, prefix + ': reference decoded offsets')
        probes = [(0, min(17, len(canonical))), (max(0, len(canonical) - 17), min(17, len(canonical)))]
        for boundary in decoded_offsets[1:len(groups)]:
            left = max(0, boundary - 5)
            probes.append((left, min(13, len(canonical) - left)))
        for i, (offset, size) in enumerate(probes):
            target = self.output / (prefix + '.range-' + str(i))
            self.reference(prefix + ' reference range ' + str(i), 'range', archive, offset, size, target)
            need(target.read_bytes() == canonical[offset:offset + size], prefix + ': reference range bytes')
        for frame_no in sorted({0, len(groups) - 1, len(groups)}):
            target = self.output / (prefix + '.reference-frame-' + str(frame_no))
            self.reference(prefix + ' reference frame ' + str(frame_no), 'frame', archive, frame_no, target)
            expected = b'' if frame_no == len(groups) else expected_frames[frame_no]
            need(target.read_bytes() == expected, prefix + ': reference frame bytes')
        self.cases.append({'name': prefix, 'status': 'PASS', 'records': len(items),
                           'group_counts': counts, 'canonical': pin(source), 'archive': pin(archive)})
        return blob, layout

    def bad_archive(self, label, blob):
        archive = self.output / ('bad-' + label + '.zst')
        archive.write_bytes(blob)
        self.candidate(label + ' rejected by verify', 'verify', archive, reject=True)
        target = self.output / ('bad-' + label + '.out')
        self.candidate(label + ' rejected by get', 'get', archive, 0, target, reject=True)
        need(not target.exists(), label + ': failed get created output')
        self.cases.append({'name': label, 'status': 'REJECTED_AS_EXPECTED', 'archive': pin(archive)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--reference', required=True, type=Path)
    parser.add_argument('--zstd', default='/usr/bin/zstd', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    driver = Driver(args)
    controlled = [Path(__file__), Path(__file__).resolve().parents[1] / 'docs' / 'FORMAT.md',
                  Path(frozen.__file__), Path(container.__file__),
                  args.candidate, args.reference, args.zstd]
    before = {str(path.resolve()): pin(path) for path in controlled}
    result = {'status': 'FAILED', 'scope': 'small synthetic correctness and two independent codec/reference contracts', 'source_before': before}
    try:
        supplements = {
            'exact_group_fill': [b'x' * (frozen.BLOCK_TARGET - 24), b'', b'', b'z'],
            'one_byte_over_group': [b'x' * (frozen.BLOCK_TARGET - 24), b'', b'z'],
        }
        need([len(group) for group in frozen.expected_groups(supplements['exact_group_fill'])] == [3, 1], 'exact-fill supplement oracle')
        need([len(group) for group in frozen.expected_groups(supplements['one_byte_over_group'])] == [2, 1], 'over-fill supplement oracle')
        saved = {}
        fixtures = [(name, list(frozen.records(name))) for name in frozen.CORRECTNESS_NAMES] + list(supplements.items())
        for name, items in fixtures:
            for mode in ('grouped', 'per-record', 'monolithic'):
                saved[(name, mode)] = driver.valid(name, items, mode)
        for name, malformed in frozen.MALFORMED_CANONICAL.items():
            source = args.output / ('invalid-' + name + '.records')
            source.write_bytes(malformed)
            target = args.output / ('invalid-' + name + '.zst')
            driver.candidate(name + ' pack rejection', 'pack', 'grouped', source, target, reject=True)
            need(target.read_bytes() == b'', name + ': initial failure output must be empty')
        source = args.output / 'oversized-record.records'
        source.write_bytes(b''.join(frozen.canonical_chunks(frozen.records('rejected_record'))))
        driver.candidate('oversized complete record rejection', 'pack', 'grouped', source, args.output / 'oversized.zst', reject=True)
        need((args.output / 'oversized.zst').read_bytes() == b'', 'oversized failure output must be empty')
        recovery_source = args.output / 'partial-pack.records'
        recovery_source.write_bytes(b''.join(frozen.canonical_chunks([b'first record', b'second record'])) + b'\x01')
        recovery_target = args.output / 'partial-pack.zst'
        driver.candidate('partial pack recovery contract', 'pack', 'per-record', recovery_source, recovery_target, reject=True)
        need(recovery_target.stat().st_size > 0, 'partial pack must preserve its written frame')
        driver.candidate('partial pack is not a complete archive', 'verify', recovery_target, reject=True)
        driver.cases.append({'name': 'new-output-partial-pack', 'status': 'PARTIAL_OUTPUT_RETAINED_AND_REJECTED',
                             'archive': pin(recovery_target)})
        original, layout = saved[('binary', 'grouped')]
        cuts = sorted({0, 1, 7, layout['data_frames'][0]['compressed_size'] - 1,
                       layout['index_frame']['offset'], layout['index_frame']['offset'] + 7,
                       layout['seek_offset'] - 1, layout['seek_offset'], layout['footer_offset'], len(original) - 1})
        for cut in cuts:
            driver.bad_archive('truncate-' + str(cut), original[:cut])
        def flip(label, offset, mask=1):
            changed = bytearray(original)
            changed[offset] ^= mask
            driver.bad_archive(label, bytes(changed))
        flip('terminal-magic', len(original) - 1)
        flip('reserved-descriptor', layout['footer_offset'] + 4, 4)
        flip('checksum-descriptor-without-fields', layout['footer_offset'] + 4, 128)
        flip('ordinal-tag', layout['index_frame']['offset'] + 8)
        flip('outer-payload-size', layout['index_frame']['offset'] + 4)
        flip('data-checksum', layout['data_frames'][0]['compressed_size'] - 1)
        flip('inner-index-checksum', layout['seek_offset'] - 1)
        flip('compressed-size', layout['seek_offset'] + 8)
        flip('decoded-size', layout['seek_offset'] + 12)
        driver.bad_archive('trailing-bytes', original + b'junk')
        for name, offset, value in [
            ('extreme-frame-count', layout['footer_offset'], 0xffffffff),
            ('extreme-compressed-size', layout['seek_offset'] + 8, 0xffffffff),
            ('extreme-decoded-size', layout['seek_offset'] + 12, 0xffffffff),
            ('above-reference-frame-limit', layout['seek_offset'] + 12, 0x40000001),
        ]:
            changed = bytearray(original)
            struct.pack_into('<I', changed, offset, value)
            driver.bad_archive(name, bytes(changed))
        def repack_top_level(frames):
            entries = b''.join(struct.pack('<II', frame['compressed_size'], frame['decompressed_size']) for frame in frames)
            table = entries + struct.pack('<IBI', len(frames), 0, frozen.SEEK_END_MAGIC)
            return (b''.join(bytes(frame['bytes']) for frame in frames)
                    + struct.pack('<II', frozen.SEEK_MAGIC, len(table)) + table)
        driver.bad_archive('missing-index', repack_top_level(layout['data_frames']))
        driver.bad_archive('duplicate-index', repack_top_level(layout['data_frames'] + [layout['index_frame']] * 2))
        for name, counts in [('no-data-index', []), ('false-empty-index', [0]),
                             ('two-zero-counts', [0, 0]), ('zero-then-one', [0, 1]),
                             ('wrong-record-count', [1]), ('extreme-record-count', [(1 << 64) - 1])]:
            packed = driver.compress_index(name, frozen.index_plain(counts))
            driver.bad_archive(name, container.rebuild_index(layout, packed))
        for name, offset, value in [('unknown-version', 8, 2), ('nonzero-index-flags', 12, 1)]:
            plain = bytearray(frozen.index_plain([4]))
            struct.pack_into('<I', plain, offset, value)
            packed = driver.compress_index(name, bytes(plain))
            driver.bad_archive(name, container.rebuild_index(layout, packed))
        boundary_blob, boundary_layout = saved[('boundaries', 'grouped')]
        zero_counts = [1] * len(boundary_layout['data_frames'])
        zero_counts[0], zero_counts[1] = 0, 2
        packed = driver.compress_index('zero-count-with-matching-size', frozen.index_plain(zero_counts))
        driver.bad_archive('zero-count-with-matching-size', container.rebuild_index(boundary_layout, packed))
        canonical_input = args.output / 'binary-grouped.records'
        valid_archive = args.output / 'binary-grouped.zst'
        for operation in ('pack', 'unpack', 'get'):
            for alias in (False, True):
                label = operation + ('-hardlink' if alias else '-sentinel')
                source = canonical_input if operation == 'pack' else valid_archive
                target = args.output / ('preserve-' + label)
                if alias:
                    os.link(source, target)
                else:
                    target.write_bytes(b'existing destination must survive\0\xff')
                source_pin, target_pin = pin(source), pin(target)
                arguments = (['pack', 'grouped', source, target] if operation == 'pack'
                             else ['unpack', source, target] if operation == 'unpack'
                             else ['get', source, 0, target])
                driver.candidate(label + ' output refusal', *arguments, reject=True)
                need(pin(source) == source_pin and pin(target) == target_pin, label + ': existing data changed')
                driver.cases.append({'name': label, 'status': 'REJECTED_WITH_FILES_PRESERVED'})
        after = {str(path.resolve()): pin(path) for path in controlled}
        need(before == after, 'controlled source or executable changed')
        result.update(status='PASS', cases=driver.cases, call_count=len(driver.calls), source_after=after,
                      format_and_sources_unchanged=True,
                      supplement='Static-review exact-fill, B+1 group and new-output recovery cases; record/query/codec definitions unchanged')
    except BaseException as error:
        result.update(error=type(error).__name__ + ': ' + str(error), cases=driver.cases, call_count=len(driver.calls))
        (args.output / 'FAILURE.txt').write_text(traceback.format_exc())
    with (args.output / 'RESULT.json').open('x') as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write('\n')
    print(json.dumps({key: value for key, value in result.items() if key != 'cases'}, sort_keys=True))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
