"""Independent frozen-layout parser; codec and record checks belong to the caller."""
import struct

ZSTD_MAGIC = 0xFD2FB528
INDEX_MAGIC = b'ARIDX001'
INDEX_SKIP_MAGIC = 0x184D2A5D
SEEK_MAGIC = 0x184D2A5E
SEEK_END_MAGIC = 0x8F92EAB1
MAX_FRAME_COUNT = 0x8000000
MAX_DECODED_FRAME = 1 << 30
U32 = struct.Struct('<I')
ENTRY = struct.Struct('<II')


class LayoutError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise LayoutError(reason)


def parse_layout(data):
    """Return immutable frame views and field offsets from saved archive bytes.

    Each data frame and the inner index still require an ordinary zstd decoder
    that checks the checksum, consumes exactly its slice and yields the stated
    decoded length. The caller compares decoded index and record bytes with the
    frozen fixtures. No decoded record or index payload is parsed here.
    """
    require(isinstance(data, bytes), 'archive must be immutable bytes')
    require(len(data) >= 17, 'truncated seek table')
    view = memoryview(data)
    footer_offset = len(data) - 9
    frame_count = U32.unpack_from(data, footer_offset)[0]
    descriptor_offset = footer_offset + 4
    require(data[descriptor_offset] == 0, 'unsupported seek descriptor')
    require(U32.unpack_from(data, footer_offset + 5)[0] == SEEK_END_MAGIC,
            'invalid terminal seek magic')
    require(2 <= frame_count <= MAX_FRAME_COUNT, 'invalid top-level frame count')
    data_frame_count = frame_count - 1
    index_decoded_size = 32 + 8 * data_frame_count
    require(index_decoded_size <= MAX_DECODED_FRAME, 'ordinal index exceeds decoded limit')

    # Check the full table extent before iterating or allocating frame metadata.
    table_payload_size = 8 * frame_count + 9
    table_offset = len(data) - 8 - table_payload_size
    require(table_offset >= 0, 'truncated seek entries')
    require(U32.unpack_from(data, table_offset)[0] == SEEK_MAGIC,
            'invalid seek-table frame magic')
    require(U32.unpack_from(data, table_offset + 4)[0] == table_payload_size,
            'seek-table payload size differs')
    entries_offset = table_offset + 8
    require(entries_offset + 8 * frame_count == footer_offset,
            'seek-table entry extent differs')

    frames = []
    offset = 0
    for number in range(frame_count):
        entry_offset = entries_offset + 8 * number
        compressed, decoded = ENTRY.unpack_from(data, entry_offset)
        end = offset + compressed
        require(compressed >= 4 and end <= table_offset, 'frame extent outside data region')
        require(decoded <= MAX_DECODED_FRAME, 'frame exceeds decoded limit')
        frame = {'number': number, 'offset': offset, 'end': end,
                 'compressed_size': compressed, 'decompressed_size': decoded,
                 'compressed_size_field_offset': entry_offset,
                 'decompressed_size_field_offset': entry_offset + 4,
                 'bytes': view[offset:end]}
        if number < data_frame_count:
            require(U32.unpack_from(data, offset)[0] == ZSTD_MAGIC,
                    'unexpected top-level data frame')
            require(decoded != 0 or data_frame_count == 1,
                    'empty data frame outside sole-frame archive')
        else:
            require(decoded == 0, 'ordinal metadata must have zero decoded table size')
            require(compressed >= 20, 'truncated ordinal-index frame')
            require(U32.unpack_from(data, offset)[0] == INDEX_SKIP_MAGIC,
                    'missing final ordinal-index frame')
            payload_size = U32.unpack_from(data, offset + 4)[0]
            require(payload_size == compressed - 8, 'ordinal outer payload size differs')
            require(data[offset + 8:offset + 16] == INDEX_MAGIC,
                    'invalid ordinal outer tag')
            inner_offset = offset + 16
            require(U32.unpack_from(data, inner_offset)[0] == ZSTD_MAGIC,
                    'invalid ordinal inner frame magic')
            frame.update({'payload_size_field_offset': offset + 4,
                          'outer_tag_offset': offset + 8,
                          'payload_size': payload_size,
                          'inner_offset': inner_offset,
                          'expected_inner_decoded_size': index_decoded_size,
                          'inner_bytes': view[inner_offset:end]})
        frames.append(frame)
        offset = end
    require(offset == table_offset, 'unaccounted bytes before seek table')

    return {'data_frames': frames[:-1], 'index_frame': frames[-1],
            'seek_offset': table_offset, 'table': view[table_offset:],
            'table_payload_size_field_offset': table_offset + 4,
            'table_payload_size': table_payload_size,
            'entries_offset': entries_offset,
            'entries': [{'offset': frame['compressed_size_field_offset'],
                         'compressed_size': frame['compressed_size'],
                         'decompressed_size': frame['decompressed_size']}
                        for frame in frames],
            'footer_offset': footer_offset,
            'frame_count_field_offset': footer_offset,
            'descriptor_offset': descriptor_offset,
            'terminal_magic_offset': footer_offset + 5,
            'frame_count': frame_count, 'data_frame_count': data_frame_count}


def rebuild_index(layout, compressed_inner):
    """Replace index bytes for negative cases while preserving valid outer framing."""
    require(isinstance(compressed_inner, bytes) and len(compressed_inner) >= 4,
            'replacement inner frame must be bytes')
    require(U32.unpack_from(compressed_inner)[0] == ZSTD_MAGIC,
            'replacement inner frame magic differs')
    frames = layout['data_frames']
    require(len(frames) == layout['data_frame_count'], 'layout data count differs')
    count = len(frames) + 1
    require(count == layout['frame_count'] and 2 <= count <= MAX_FRAME_COUNT,
            'layout top-level count differs')
    require(32 + 8 * len(frames) <= MAX_DECODED_FRAME, 'layout index exceeds decoded limit')
    payload = INDEX_MAGIC + compressed_inner
    require(len(payload) + 8 <= 0xffffffff, 'replacement outer frame exceeds entry size')
    outer = ENTRY.pack(INDEX_SKIP_MAGIC, len(payload)) + payload
    chunks, entries = [], []
    for frame in frames:
        require(len(frame['bytes']) == frame['compressed_size'], 'layout data size differs')
        chunks.append(frame['bytes'])
        entries.append(ENTRY.pack(frame['compressed_size'], frame['decompressed_size']))
    entries.append(ENTRY.pack(len(outer), 0))
    table_payload = b''.join(entries) + struct.pack('<IBI', count, 0, SEEK_END_MAGIC)
    table = ENTRY.pack(SEEK_MAGIC, len(table_payload)) + table_payload
    return b''.join(chunks) + outer + table
