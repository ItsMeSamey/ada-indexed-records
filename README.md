# Ada indexed records

Store opaque binary records in compressed groups and read one record by its zero-based ordinal. This suits saved results, logs and fixtures where repeated content compresses well and individual records must remain accessible.

The tested defaults are a 256 KiB group target and a 2 MiB record limit. Grouping trades random-read speed for smaller archives. The API also supports one frame per record and a single-frame layout.

See the [API](docs/API.md), [binary format](docs/FORMAT.md) and [full benchmark comparison](docs/BENCHMARKS.md).

## Use

`Indexed_Records.Bytes` is an `Ada.Streams.Stream_Element_Array`.

| Operation | API |
| --- | --- |
| Write records | `Create`, `Append`, `Close` on a `Writer` |
| Read a record | `Open`, `Read (Reader, Ordinal)`, `Close` |
| Get record count | `Count` |
| Check all frames and records | `Validate` |
| Restore the canonical stream | `Unpack` |
| Inspect storage and read costs | `Archive_Bytes`, `Index_Bytes`, `Bytes_Read`, `Bytes_Decoded` |

Call `Abort_Write` to close an interrupted writer. Output operations require a new destination file.

The CLI accepts these commands:

```sh
bin/records_cli pack grouped records.bin records.zst
bin/records_cli info records.zst
bin/records_cli get records.zst 0 first-record.bin
bin/records_cli verify records.zst
bin/records_cli unpack records.zst restored.bin
```

`pack` also accepts `per-record` and `monolithic`. Its input and `unpack` output use the canonical stream below. `get` writes only the selected payload. Empty payloads produce empty output files.

A failed command exits nonzero and may leave an empty or partial new output. Use an output only after success. Inspect and remove that owned partial file, or choose a new destination before retrying.

## Format and validation

The canonical stream repeats an eight-byte unsigned little-endian payload length followed by the exact payload bytes. Empty records retain their zero length. NUL bytes, line endings and binary data pass through unchanged.

With the CLI and API defaults, grouped mode adds whole serialized records until the next record would exceed 262,144 bytes. A larger record occupies its own frame. The default payload limit is 2,097,152 bytes; the [API settings and limits](docs/API.md) describe configurable values. Each zstd frame uses level 3, zero workers, a content-size field and a content checksum. Empty input has one empty data frame.

After the data frames, an `ARIDX001` ordinal index stores record counts per frame inside a checksummed zstd frame, wrapped in a skippable frame. A standard zstd seek table closes the archive and includes the zero-output index frame. Ordinary zstd decoding restores the canonical stream; the official seekable reader can address its byte ranges.

`Open` checks the table and ordinal index. `Read` locates a group, decompresses it and checks its checksum, size and record boundaries. `Validate` checks every data frame and record. Size, count, truncation and checksum errors fail explicitly. Each read decodes the selected frame afresh.

## Measured tradeoff

Four fixed synthetic corpora each contain 65,536 records. Every layout passed full-stream and query-byte comparisons. These medians pool three ordered passes on the same query sequence; query timers exclude payload-file writes.

| Corpus | Grouped bytes | Per-record bytes | Grouped read us | Per-record read us | Decoded-cache read us |
| --- | ---: | ---: | ---: | ---: | ---: |
| Tiny repeated | 515 | 2,359,425 | 441.543 | 6.010 | 0.592 |
| Mixed repeated | 39,649 | 2,799,426 | 77.626 | 5.515 | 0.554 |
| Mixed entropy | 88,349,313 | 89,734,958 | 86.450 | 5.395 | 0.934 |
| Cross-record repetition | 183,629 | 3,323,259 | 64.042 | 5.772 | 0.873 |

Per-record frames read faster in all four cases. A fully decoded cache is faster again after loading the whole stream and building offsets. Grouping saves substantial storage on repeated data. For mixed entropy, the canonical input is 88,339,849 bytes; grouping adds 9,464 bytes. The raw-plus-offsets control occupies 88,864,137 bytes. The uncached monolithic control stores mixed repeated data in 8,249 bytes but takes 66,962.194 us per median read.

The three compressed layouts use the same Ada codec with libzstd 1.5.7. Raw, ZIP and decoded-cache controls use Python; ZIP uses one DEFLATE entry per record. Measurements used a shared host with its OS cache retained. First-pass and warm-read results differ. A separate run measured process VmHWM/VmRSS for all 24 cases; the full comparison gives its scope.

## Build and test

Install GNAT, GPRbuild and the system zstd development library (>=1.5.5, <2). Tests also need Python 3, the `zstd` CLI and a C compiler (`gcc` by default) for the bundled reference reader. With those tools configured in `PATH`, run from the release root:

```sh
gprbuild -p -P records_cli.gpr
python3 -I -B scripts/test.py --output .checks-a01
```

The build creates `bin/records_cli`. The test script builds the CLI and reference reader, then checks the fixed small fixtures. Its output directory must be new. For the optional four-corpus storage/read comparison:

```sh
python3 -I -B scripts/benchmark.py --output .bench-a01
```

For a separate Linux process-memory pass over that completed benchmark:

```sh
python3 -I -B scripts/memory.py --measurements .bench-a01 --output .memory-a01
```

The memory command reuses the pinned archives, queries and expected payloads. Add `--fixture tiny_repeated` for a six-layout portability check; the result identifies that subset. Its timing fields are incidental.

Direct GNAT 16 builds, GPRbuild 26.0.0 library/CLI builds and a separate consumer round trip passed on Arch Linux x86_64 with system libzstd 1.5.7. A separate upstream libzstd 1.5.5 build passed candidate, consumer and reference checks on the same host. The consumer inherited the native link dependency from the library project.

The intended manifest availability is x86-64 on Arch, Debian and Ubuntu. Local execution is qualified on Arch; Debian and Ubuntu await the Alire index's distribution CI checks.

Alire 2.1.1 resolved the installed system library and built both projects using a local index. The included `libzstd` entry is a proposal awaiting community-index acceptance. To use it from the release root:

```sh
alr index --add "$PWD/local-index" --name indexedrecordslocal
alr build
```

## Credits

Compression uses [Zstandard](https://github.com/meta/zstd), copyright Meta Platforms, Inc. and affiliates, under its BSD-3-Clause licence option. Keep its copyright, licence conditions and disclaimer with redistributed zstd material. The separately built [official seekable-format reader](https://github.com/meta/zstd/tree/v1.5.7/contrib/seekable_format) supplied interoperability checks against zstd 1.5.7; it is a test reference, separate from the Ada library.

Project source uses the [MIT licence](LICENSE). [Third-party notices](THIRD_PARTY_NOTICES.md) retain the upstream terms and credits.
