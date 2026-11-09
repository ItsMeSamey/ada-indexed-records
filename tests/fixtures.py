"""Frozen synthetic records and independent byte/ordinal oracles; no entry imports."""
import hashlib
import struct

BLOCK_TARGET = 256 * 1024
MAX_RECORD_BYTES = 2 * 1024 * 1024
COST_RECORD_COUNT = 65536
CODEC = {'level': 3, 'workers': 0, 'checksum': 1, 'content_size': 1, 'dictionary': None}
LENGTHS = (0, 1, 7, 31, 127, 1023, 8191)
COST_NAMES = ('tiny_repeated', 'mixed_repeated', 'mixed_entropy', 'cross_record')
INDEX_MAGIC = b'ARIDX001'
INDEX_VERSION = 1
SKIP_MAGIC = 0x184D2A5D
SEEK_MAGIC = 0x184D2A5E
SEEK_END_MAGIC = 0x8F92EAB1
QUERY_SEED = 0x399A2026


def repeated(pattern, length):
    return (pattern * ((length + len(pattern) - 1) // len(pattern)))[:length]


def entropy(record, length):
    prefix = b'AdaRecords/A01/entropy\0' + struct.pack('<Q', record)
    return b''.join(hashlib.sha256(prefix + struct.pack('<I', block)).digest()
                    for block in range((length + 31) // 32))[:length]


def records(name):
    if name == 'empty':
        return
    if name == 'empty_records':
        yield from (b'', b'', b'')
        return
    if name == 'binary':
        yield from (bytes(range(256)), b'\0\n\r\t\xff', b'', bytes(range(255, -1, -1)))
        return
    if name == 'boundaries':
        lengths = (BLOCK_TARGET - 9, 0, BLOCK_TARGET - 8, 1,
                   BLOCK_TARGET - 7, 7, BLOCK_TARGET + 17, 0)
        for i, length in enumerate(lengths):
            yield repeated(bytes((i, 255 - i, 0, 10)), length)
        return
    if name == 'maximum_record':
        yield repeated(b'max\0\xff\n', MAX_RECORD_BYTES)
        yield b''
        return
    if name == 'rejected_record':
        yield repeated(b'rejected', MAX_RECORD_BYTES + 1)
        return
    if name not in COST_NAMES:
        raise ValueError('unknown fixture: ' + name)
    for i in range(COST_RECORD_COUNT):
        length = (0, 1, 7, 31)[i % 4] if name == 'tiny_repeated' else LENGTHS[i % len(LENGTHS)]
        if name == 'mixed_entropy':
            yield entropy(i, length)
        elif name == 'cross_record':
            prefix = struct.pack('<Q', i)
            yield (prefix + repeated(b' shared-proof-step payload\0\n', length))[:length]
        else:
            yield repeated(b'alpha beta gamma\0\n', length)


def canonical_chunks(items):
    for item in items:
        yield struct.pack('<Q', len(item))
        yield item


def unpack_canonical(data, maximum=MAX_RECORD_BYTES):
    """Reference parser returns exact payload slices, never normalizes bytes."""
    result = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < 8:
            raise ValueError('truncated length')
        length = struct.unpack_from('<Q', data, offset)[0]
        offset += 8
        if length > maximum:
            raise ValueError('record exceeds maximum')
        if length > len(data) - offset:
            raise ValueError('truncated payload')
        result.append(data[offset:offset + length])
        offset += length
    return result


def expected_groups(items, target=BLOCK_TARGET, maximum=MAX_RECORD_BYTES):
    """Reference groups are logical record lists; no codec or container parsing."""
    group = []
    size = 0
    seen = False
    for item in items:
        seen = True
        if len(item) > maximum:
            raise ValueError('record exceeds maximum')
        encoded = 8 + len(item)
        if group and size + encoded > target:
            yield group
            group, size = [], 0
        group.append(item)
        size += encoded
        if size > target:
            yield group
            group, size = [], 0
    if group:
        yield group
    elif not seen:
        yield []


def queries(record_count, group_counts):
    """Frozen order: endpoints, group boundaries, seeded uniform, burst, repeats."""
    if record_count == 0:
        return []
    starts = []
    total = 0
    for count in group_counts:
        if count <= 0:
            raise ValueError('nonempty fixture group must have records')
        starts.append(total)
        total += count
    if total != record_count:
        raise ValueError('group count differs')
    result = [0, record_count - 1, 0, record_count - 1]
    chosen = list(range(min(8, len(starts))))
    chosen += [i for i in range(max(8, len(starts) - 8), len(starts)) if i not in chosen]
    for i in chosen:
        result += [starts[i], starts[i] + group_counts[i] - 1]
    state = QUERY_SEED
    for _ in range(64):
        state = (1664525 * state + 1013904223) & 0xffffffff
        result.append(state % record_count)
    result += [(record_count // 3 + i) % record_count for i in range(16)]
    result += [record_count // 2] * 8
    return result


def index_plain(group_counts):
    """Exact inner ordinal-index payload; the outer tag is also INDEX_MAGIC."""
    if any(count < 0 or count >= 1 << 64 for count in group_counts):
        raise ValueError('index count range')
    total = sum(group_counts)
    if total >= 1 << 64:
        raise ValueError('record count overflow')
    return (INDEX_MAGIC + struct.pack('<IIQQ', INDEX_VERSION, 0, total, len(group_counts))
            + b''.join(struct.pack('<Q', count) for count in group_counts))


CORRECTNESS_NAMES = ('empty', 'empty_records', 'binary', 'boundaries', 'maximum_record')
REJECTION_NAMES = ('rejected_record',)
MALFORMED_CANONICAL = {
    'partial_length': b'\x01' * 7,
    'missing_payload': struct.pack('<Q', 3) + b'ab',
    'excessive_declared_length': struct.pack('<Q', MAX_RECORD_BYTES + 1),
    'u64_max_length': struct.pack('<Q', (1 << 64) - 1),
}
