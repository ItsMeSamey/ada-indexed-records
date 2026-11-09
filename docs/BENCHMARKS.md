# Storage and ordinal-read measurements

The fixed comparison used four synthetic corpora of 65,536 records each. All layouts passed exact full-stream and query-payload checks. Group sizes, codec settings, corpus bytes and query order were fixed before the run.

Grouping trades read latency for storage. Per-record frames answered random reads faster on all four corpora. A fully decoded cache was faster again after its initial load. Repeated data gave grouping a large storage benefit; grouping slightly expanded the high-entropy canonical input.

## Controls

| Layout | Stored representation and read path |
| --- | --- |
| Grouped | Whole records grouped toward 256 KiB; decode the selected frame for each query |
| Per-record | One frame per record; decode the selected frame |
| Monolithic | One data frame; decode it for each query |
| Raw | Canonical stream plus an offset file built separately and loaded at open |
| ZIP | One fixed-metadata DEFLATE entry per record |
| Cache | Monolithic archive decoded once at open, with the resident canonical stream and ordinal offsets |

Grouped, per-record and monolithic use the same Ada binding, zstd level 3, zero workers, content sizes and checksums. They retain the same ordinal-index/seek-table format. Raw, ZIP and cache use Python. ZIP measures that format and implementation; it is separate from Zipada.

Stored bytes include archive metadata. Raw storage includes the existing canonical input and a separate 524,288-byte offset file. A measured build call constructs that file; open loads and checks it. Cache stored bytes refer to its monolithic archive; its logical resident canonical-stream/offset storage is 1,687,552 bytes for tiny repeated data and 88,864,137 bytes for each other corpus. Separate process-memory results appear below.

## Results

Read medians and P95 values pool three fixed ordered passes. Each corpus uses one query sequence across every layout: endpoints, group boundaries, seeded positions, local bursts and repeats. Tiny repeated has 102 queries per pass; the other corpora have 124.

| Corpus | Layout | Stored bytes | Open ms | Median read us | P95 read us |
| --- | --- | ---: | ---: | ---: | ---: |
| Tiny repeated | Grouped | 515 | 0.052 | 441.543 | 807.728 |
| Tiny repeated | Per-record | 2,359,425 | 62.422 | 6.010 | 8.609 |
| Tiny repeated | Monolithic | 242 | 0.045 | 1,645.319 | 2,983.710 |
| Tiny repeated | Raw | 1,687,552 | 5.009 | 7.861 | 16.238 |
| Tiny repeated | ZIP | 6,635,618 | 253.892 | 28.878 | 54.193 |
| Tiny repeated | Cache | 242 | 22.086 | 0.592 | 0.895 |
| Mixed repeated | Grouped | 39,649 | 0.266 | 77.626 | 113.247 |
| Mixed repeated | Per-record | 2,799,426 | 29.934 | 5.515 | 9.127 |
| Mixed repeated | Monolithic | 8,249 | 0.187 | 66,962.194 | 95,081.268 |
| Mixed repeated | Raw | 88,864,137 | 10.894 | 25.490 | 555.676 |
| Mixed repeated | ZIP | 7,620,955 | 466.432 | 71.323 | 1,001.362 |
| Mixed repeated | Cache | 8,249 | 258.659 | 0.554 | 1.493 |
| Mixed entropy | Grouped | 88,349,313 | 0.555 | 86.450 | 183.236 |
| Mixed entropy | Per-record | 89,734,958 | 38.594 | 5.395 | 9.959 |
| Mixed entropy | Monolithic | 88,341,967 | 0.059 | 151,999.455 | 261,872.240 |
| Mixed entropy | Raw | 88,864,137 | 12.773 | 1,422.561 | 9,780.181 |
| Mixed entropy | ZIP | 94,076,523 | 409.545 | 520.552 | 7,241.043 |
| Mixed entropy | Cache | 88,341,967 | 1,086.302 | 0.934 | 1.707 |
| Cross-record repetition | Grouped | 183,629 | 0.272 | 64.042 | 75.586 |
| Cross-record repetition | Per-record | 3,323,259 | 25.432 | 5.772 | 10.095 |
| Cross-record repetition | Monolithic | 132,811 | 0.053 | 88,475.929 | 133,941.510 |
| Cross-record repetition | Raw | 88,864,137 | 12.298 | 20.713 | 25.739 |
| Cross-record repetition | ZIP | 8,189,912 | 377.399 | 36.309 | 84.781 |
| Cross-record repetition | Cache | 132,811 | 303.096 | 0.873 | 2.047 |

For mixed entropy, the canonical input is 88,339,849 bytes. Grouping stores 88,349,313 bytes, an expansion of 9,464 bytes. The raw-plus-offsets control occupies 88,864,137 bytes. Per-record framing occupies 89,734,958 bytes and ZIP occupies 94,076,523. Grouping's overhead relative to the monolithic archive is 7,346 bytes, while avoiding a full-stream decode for each read.

The smaller monolithic archives can suit full-stream reads. Per-record frames suit lower-latency ordinal access. A decoded cache suits repeated access when its load and resident storage are acceptable. Grouping occupies the middle ground for compressible records.

## Build and process costs

These are saved native-process totals in seconds. Build covers archive construction, or offset-file construction for raw. The read process includes startup, open, all three query passes, correctness-sidecar output and completion. Cache reuses the monolithic archive build; its repeated build values below represent that shared prerequisite.

| Corpus | Layout | Build wall s | Build CPU s | Read-process wall s |
| --- | --- | ---: | ---: | ---: |
| Tiny repeated | Grouped | 0.021971 | 0.009701 | 0.151835 |
| Tiny repeated | Per-record | 0.175004 | 0.171804 | 0.070720 |
| Tiny repeated | Monolithic | 0.013791 | 0.011601 | 0.566474 |
| Tiny repeated | Raw | 0.224406 | 0.185240 | 0.162888 |
| Tiny repeated | ZIP | 1.583952 | 1.551100 | 0.418788 |
| Tiny repeated | Cache | 0.013791 | 0.011601 | 0.137648 |
| Mixed repeated | Grouped | 0.038520 | 0.037619 | 0.036636 |
| Mixed repeated | Per-record | 0.248965 | 0.240484 | 0.035588 |
| Mixed repeated | Monolithic | 0.180695 | 0.173537 | 26.237526 |
| Mixed repeated | Raw | 0.819248 | 0.226099 | 0.274299 |
| Mixed repeated | ZIP | 2.488760 | 1.817861 | 0.744136 |
| Mixed repeated | Cache | 0.180695 | 0.173537 | 0.415162 |
| Mixed entropy | Grouped | 0.304828 | 0.256366 | 0.054223 |
| Mixed entropy | Per-record | 0.441683 | 0.426200 | 0.044293 |
| Mixed entropy | Monolithic | 0.695173 | 0.622391 | 62.232501 |
| Mixed entropy | Raw | 1.582438 | 0.314689 | 1.388649 |
| Mixed entropy | ZIP | 5.569987 | 4.391142 | 1.415698 |
| Mixed entropy | Cache | 0.695173 | 0.622391 | 1.353276 |
| Cross-record repetition | Grouped | 0.114324 | 0.078222 | 0.049623 |
| Cross-record repetition | Per-record | 0.360996 | 0.310105 | 0.030893 |
| Cross-record repetition | Monolithic | 0.217205 | 0.210274 | 35.440155 |
| Cross-record repetition | Raw | 1.270794 | 0.301751 | 0.267856 |
| Cross-record repetition | ZIP | 2.420010 | 2.127577 | 0.606173 |
| Cross-record repetition | Cache | 0.217205 | 0.210274 | 0.609555 |

## Conditions and timing scope

The run used direct GNAT 16 on Arch Linux x86_64 with libzstd 1.5.7. It completed 80 native calls in 176.081 seconds on a shared host. Two tool downloads overlapped; host load was recorded before each child. The OS cache remained in its normal state. Settings remained fixed after the results.

Per-query clocks cover the read and returned payload construction, with correctness-sidecar writes afterward. Ada pass clocks include those writes and metric collection, but finish before buffered file close. Python sidecar writes are unbuffered. Native totals include startup and process completion; Ada CPU clocks measure the current task. Application byte counters count requested and decoded bytes rather than physical device I/O.

First-pass and warm-pass behavior varies. For grouped mixed repeated data, median latency was 93.246 us on pass one and 47.642 us across later passes; tiny repeated was 341.707 us then 486.121 us. The aggregate table retains those differing outcomes rather than assuming every repeated pass improves.

## Process memory

A separate run reused the same archives and queries for all 24 cases. It recorded `/proc/self/status` VmHWM and VmRSS at worker start, after open, and after the three query passes. Every case passed exact query-payload checks. The final VmHWM values below are MiB (1 MiB = 1,048,576 bytes), sampled after queries and before file/reader closure. The storage/read timings above come from the separate timing pass.

| Corpus | Grouped | Per-record | Monolithic | Raw | ZIP | Cache worker |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Tiny repeated | 3.69 | 7.53 | 6.36 | 19.48 | 55.41 | 20.86 |
| Mixed repeated | 3.50 | 7.13 | 255.45 | 19.50 | 55.94 | 186.76 |
| Mixed entropy | 4.08 | 7.14 | 339.32 | 19.39 | 56.88 | 187.20 |
| Cross-record repetition | 3.75 | 7.47 | 255.72 | 19.65 | 56.28 | 187.29 |

These are approximate kernel-reported process highwaters, including each worker's runtime, metadata and buffers. Ada and Python runtime costs remain part of the comparison. The cache worker also launches a zstd subprocess; its separate child-lifetime peak records remain distinct from these worker values and from a simultaneous process-tree peak. Timing fields from this memory run are incidental.
