# Indexed-record archive format, version 1

All integers below are unsigned and little-endian. Offsets are zero-based. Records contain opaque bytes and use zero-based ordinals.

## Canonical record stream

Each record is `uint64 payload_length` followed by exactly that many payload bytes. An empty record consists of eight zero bytes. An empty stream has zero records. Payloads retain their original bytes and order.

The tested writer uses a 262,144-byte group target and a 2,097,152-byte payload limit. It adds whole serialized records greedily. Before a record would exceed the target, it flushes the current nonempty group. A serialized record larger than the target occupies its own frame. Empty input produces one empty data frame with record count zero.

## Top-level layout

```text
zstd data frame 0
...
zstd data frame D-1
ordinal-index skippable frame
zstd seek-table skippable frame
```

`D >= 1`. Every nonempty archive data frame contains at least one complete record. Each data frame is a standard zstd frame with known content size, content checksum and no dictionary. The writer uses compression level 3 and zero workers. Its decoded content is a sequence of whole canonical records.

The two metadata frames are skippable. Ordinary zstd decoding therefore restores the canonical record stream.

## Ordinal-index frame

The outer skippable frame has this layout:

| Offset | Bytes | Value |
| ---: | ---: | --- |
| 0 | 4 | Magic `0x184D2A5D` (`5D 2A 4D 18`) |
| 4 | 4 | Payload size: `8 + C`, where `C` is the inner compressed-frame size |
| 8 | 8 | ASCII `ARIDX001` |
| 16 | C | One checksummed zstd frame containing the index below |

Its complete size is `16 + C`. The inner frame uses the same codec settings as a data frame. Its decoded size is exactly `32 + 8*D` bytes:

| Decoded offset | Bytes | Value |
| ---: | ---: | --- |
| 0 | 8 | ASCII `ARIDX001` |
| 8 | 4 | Version: `1` |
| 12 | 4 | Flags: `0` |
| 16 | 8 | Total record count |
| 24 | 8 | Data-frame count: `D` |
| 32 + 8*i | 8 | Record count in data frame `i`, for `0 <= i < D` |

The count sum must equal the total. Cumulative counts map an ordinal to its data frame. Within that frame, record lengths locate the payload. The sole zero-count case is one empty data frame and a zero total.

## Final seek table

Let `T = D + 1`: all data frames plus the ordinal-index outer frame. The final seek table is:

| Offset | Bytes | Value |
| ---: | ---: | --- |
| 0 | 4 | Magic `0x184D2A5E` (`5E 2A 4D 18`) |
| 4 | 4 | Payload size: `8*T + 9` |
| 8 + 8*i | 4 | Complete compressed size of preceding top-level frame `i` |
| 12 + 8*i | 4 | Decompressed size of that frame |
| 8 + 8*T | 4 | Frame count: `T` |
| 12 + 8*T | 1 | Descriptor: `0` |
| 13 + 8*T | 4 | Terminal magic `0x8F92EAB1` (`B1 EA 92 8F`) |

The table's complete size is `17 + 8*T`. Entries cover `0 <= i < T`; the table excludes itself. The ordinal-index entry has compressed size `16 + C` and decompressed size zero. The inner index frame remains part of that skippable payload. Descriptor zero selects eight-byte entries without optional seek-table checksum fields; data and inner-index zstd checksums remain required.

## Validation

The reader checks exact file extent, magic/version/flags, bounded table/index/frame sizes, count sums and arithmetic overflow. The sum of preceding compressed sizes must reach the seek table exactly. Each accessed data frame must decode to the advertised size and contain the advertised number of complete records, each within the configured payload limit.

The implementation caps decoded frames at 1 GiB and seek-table entries at `0x08000000`. Reader metadata limits are described in [API.md](API.md). `Open` validates metadata and frame headers; `Read` validates the selected frame; `Validate` checks every frame. Trailing bytes, malformed lengths, invalid checksums and out-of-range ordinals fail explicitly.

The standard seek-table layout follows the [zstd 1.5.7 seekable-format specification](https://github.com/meta/zstd/blob/v1.5.7/contrib/seekable_format/zstd_seekable_compression_format.md). The separately built official reader passed the tested frame-offset, size and cross-boundary byte-range checks, including empty data and the zero-output index frame.
