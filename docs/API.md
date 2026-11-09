# API

`Indexed_Records` stores opaque byte arrays. `Bytes` is an `Ada.Streams.Stream_Element_Array`; `U64` is an unsigned 64-bit integer. Record ordinals start at zero. Frame-inspection indices start at one.

## Write lifecycle

```ada
Create
  (W, Path,
   Block_Target => 262_144,
   Max_Record_Bytes => 2_097_152,
   Mode => Grouped);
Append (W, Payload);
Close (W);
```

The destination must be new. Append records in their intended order, then call `Close` to write the ordinal index and seek table. Treat the archive as immutable after close. A new archive is required to change its records.

`Grouped` packs whole serialized records up to `Block_Target`. A larger record gets its own frame. `Per_Record` writes one frame per record. `Monolithic` collects all records in one frame, subject to the frame-size limit. Empty input produces one empty frame in every mode.

On failure, call `Abort_Write` to close the file and free the writer buffer. It preserves the partial file for inspection; only successful `Close` completes an archive. Use a new path for a later attempt.

## Read lifecycle

```ada
Open (R, Path,
      Max_Index_Bytes => 67_108_864,
      Max_Record_Bytes => 2_097_152);
-- Count (R) gives the number of records.
-- Read (R, Ordinal) returns an independent payload array.
Validate (R);
Close (R);
```

Keep the underlying archive unchanged while a reader is open. Serialize operations on each reader because they share a file position and counters.

`Open` loads the seek table and ordinal index, checks their extents and counts, verifies the index checksum and checks data-frame headers. Its work and table storage grow with the frame count.

`Read` checks the ordinal range and uses binary search over cumulative frame counts. It then decompresses the selected frame, checks the frame checksum and exact size, validates every record boundary/count in that frame, and returns the requested payload. Lookup is logarithmic in frame count; decoding and the record scan depend on the selected frame. Each call decodes that frame afresh. An empty archive has no valid ordinal.

`Validate` decodes and checks every data frame and record. Use it when complete archive validation is required before consuming results. `Unpack (R, Output_Path)` performs the same frame checks while writing the full canonical stream to a new file.

## Limits

| Setting | Default | Scope |
| --- | ---: | --- |
| `Block_Target` | 262,144 bytes | Writer's grouped-frame target, including eight-byte record lengths |
| `Max_Record_Bytes` | 2,097,152 bytes | Writer payload limit; reader payload limit during frame parsing |
| `Max_Index_Bytes` | 67,108,864 bytes | Reader checks the seek table, encoded ordinal-index frame and decoded ordinal index separately |

The format implementation also limits decoded frames to 1 GiB and seek-table entries to `0x08000000`. Compressed-size fields are unsigned 32-bit values. The writer requires `Block_Target <= 1 GiB` and `Max_Record_Bytes + 8 <= 1 GiB`.

The group target controls archives made by this writer. A reader can encounter a larger valid frame, up to the format limit. Actual working memory includes metadata, compressed/decoded buffers and the returned payload.

## Inspection and errors

| Function | Value |
| --- | --- |
| `Count` | Total record count |
| `Frame_Count` | Data-frame count, excluding metadata |
| `Compressed_Size`, `Decoded_Size` | Sizes for a one-based data-frame index |
| `Archive_Bytes` | Complete file size |
| `Index_Bytes` | Ordinal-index frame plus final seek table |
| `Bytes_Read` | Archive bytes requested by completed library reads since `Open` |
| `Bytes_Decoded` | Decoded index/data bytes counted after successful decoding since `Open` |

Counters describe application work. The operating system may serve reads from cache.

Structural errors raise the public exception `Indexed_Records.Format_Error`. Internal codec failures and Ada file/allocation exceptions may also propagate. Close the reader in exception handlers before re-raising. Existing output paths are refused.

Failed write or unpack operations may leave an empty or partial new file. Only successful writer `Close` completes an archive. Use outputs after successful completion; inspect and remove an owned partial file, or choose a new path before retrying. CLI failures return a nonzero exit status.

The CLI exposes packing, ordinal reads, full validation, unpacking and metadata inspection. `get` writes a payload; `unpack` writes length-prefixed canonical records.
