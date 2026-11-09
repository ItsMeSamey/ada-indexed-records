#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "zstd_seekable.h"

static uint64_t number(const char *s) {
    char *end = NULL;
    if (!*s || *s == '-') { fprintf(stderr, "invalid integer\n"); exit(2); }
    errno = 0;
    unsigned long long n = strtoull(s, &end, 10);
    if (errno || !end || *end) { fprintf(stderr, "invalid integer\n"); exit(2); }
    return n;
}

int main(int argc, char **argv) {
    int status = 1;
    FILE *input = NULL, *output = NULL;
    ZSTD_seekable *reader = NULL;
    unsigned char *buffer = NULL;
    size_t result;
    if (argc < 3) { fprintf(stderr, "info ARCHIVE | range ARCHIVE OFFSET LENGTH OUTPUT | frame ARCHIVE INDEX OUTPUT\n"); return 2; }
    input = fopen(argv[2], "rb");
    if (!input) { perror("open archive"); goto done; }
    if (!strcmp(argv[1], "frame-info") && argc == 3) {
        if (fseek(input, 0, SEEK_END)) goto done;
        long extent = ftell(input);
        if (extent < 0 || fseek(input, 0, SEEK_SET)) goto done;
        buffer = malloc(extent ? (size_t)extent : 1);
        if (!buffer || fread(buffer, 1, (size_t)extent, input) != (size_t)extent) goto done;
        size_t first = ZSTD_findFrameCompressedSize(buffer, (size_t)extent);
        unsigned long long content = ZSTD_getFrameContentSize(buffer, (size_t)extent);
        if (ZSTD_isError(first) || content == ZSTD_CONTENTSIZE_ERROR || content == ZSTD_CONTENTSIZE_UNKNOWN) {
            fprintf(stderr, "invalid or unknown frame extent\n"); goto done;
        }
        printf("{\"compressed_size\":%zu,\"content_size\":%llu}\n", first, content);
        status = 0; goto done;
    }
    reader = ZSTD_seekable_create();
    if (!reader) { fprintf(stderr, "reference allocation failed\n"); goto done; }
    result = ZSTD_seekable_initFile(reader, input);
    if (ZSTD_isError(result)) { fprintf(stderr, "reference init: %s\n", ZSTD_getErrorName(result)); goto done; }
    unsigned frames = ZSTD_seekable_getNumFrames(reader);
    if (!strcmp(argv[1], "info") && argc == 3) {
        printf("{\"frame_count\":%u,\"frames\":[", frames);
        for (unsigned i = 0; i < frames; i++) {
            printf("%s{\"compressed_offset\":%llu,\"decompressed_offset\":%llu,\"compressed_size\":%zu,\"decompressed_size\":%zu}",
                   i ? "," : "", ZSTD_seekable_getFrameCompressedOffset(reader,i),
                   ZSTD_seekable_getFrameDecompressedOffset(reader,i),
                   ZSTD_seekable_getFrameCompressedSize(reader,i),
                   ZSTD_seekable_getFrameDecompressedSize(reader,i));
        }
        printf("]}\n"); status = 0; goto done;
    }
    uint64_t offset = 0, length;
    unsigned frame = 0;
    const char *path;
    int by_frame = !strcmp(argv[1], "frame");
    if (by_frame && argc == 5) {
        uint64_t n = number(argv[3]);
        if (n >= frames) { fprintf(stderr, "frame out of range\n"); goto done; }
        frame = (unsigned)n;
        length = ZSTD_seekable_getFrameDecompressedSize(reader,frame);
        path = argv[4];
    } else if (!strcmp(argv[1], "range") && argc == 6) {
        offset = number(argv[3]); length = number(argv[4]); path = argv[5];
        uint64_t total = frames ? ZSTD_seekable_getFrameDecompressedOffset(reader,frames-1) +
                                   ZSTD_seekable_getFrameDecompressedSize(reader,frames-1) : 0;
        if (offset > total || length > total-offset) { fprintf(stderr, "range outside data\n"); goto done; }
    } else { fprintf(stderr, "invalid command\n"); goto done; }
    if (length > SIZE_MAX) { fprintf(stderr, "size overflow\n"); goto done; }
    buffer = malloc(length ? (size_t)length : 1);
    if (!buffer) { fprintf(stderr, "allocation failed\n"); goto done; }
    result = by_frame ? ZSTD_seekable_decompressFrame(reader,buffer,(size_t)length,frame)
                      : ZSTD_seekable_decompress(reader,buffer,(size_t)length,offset);
    if (ZSTD_isError(result) || result != length) {
        fprintf(stderr, "reference decode: %s\n", ZSTD_isError(result) ? ZSTD_getErrorName(result) : "short output"); goto done;
    }
    output = fopen(path,"wb");
    if (!output) { perror("open output"); goto done; }
    if (fwrite(buffer,1,(size_t)length,output) != length || fflush(output)) { perror("write output"); goto done; }
    status = 0;
done:
    if (output && fclose(output)) status = 1;
    free(buffer);
    if (reader) ZSTD_seekable_free(reader);
    if (input) fclose(input);
    return status;
}
