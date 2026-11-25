#!/usr/bin/env python3
"""
jdlz.py — Pure-Python EA Black Box JDLZ v2 decompressor & compressor.

Matches the exact bitstream semantics of `JLZDecompress` (@0x0031C778 in PS2
Need for Speed: Most Wanted Black Edition `SLUS_213.51`):

16-byte LZHeader (little-endian):
    0x00  char[4]  magic              b"JDLZ" (0x5A4C444A)
    0x04  uint8    version            2
    0x05  uint8    header_size        16 (0x10)
    0x06  uint16   flags              0
    0x08  uint32   uncompressed_size  exact decoded byte length
    0x0C  uint32   compressed_size    total byte length including 16-byte header

Payload stream (starting at offset 16):
    Two independent 8-bit LSB-first flag streams (`flags1` / `control` and
    `flags2` / `runtype`) are interleaved inline with tokens. The first two
    payload bytes seed `control` (byte 16) and `runtype` (byte 17).
    - `control` bit == 0: 1-byte literal.
    - `control` bit == 1: 2-byte match (b0, b1), consuming 1 bit of `runtype`:
        * `runtype` bit == 1 (near / RLE):
            offset = (b0 & 0x0F) + 1                 in [1..16]
            length = (((b0 >> 4) << 8) | b1) + 3     in [3..4098]
        * `runtype` bit == 0 (far / window):
            length = (b0 & 0x1F) + 3                 in [3..34]
            offset = (((b0 >> 5) << 8) | b1) + 17    in [17..2064]
    Whenever `control` exhausts its 8 bits, the next byte in the stream reloads
    `control` FIRST; if `runtype` also exhausted its 8 bits on the same token,
    the following byte reloads `runtype` SECOND.
"""

from __future__ import annotations

import argparse
import dataclasses
import struct
import sys
from pathlib import Path
from typing import Dict, List, Tuple

JDLZ_MAGIC = b"JDLZ"
RAWW_MAGIC = b"RAWW"
JDLZ_VERSION = 2
JDLZ_HEADER_SIZE = 16

NEAR_MAX_OFFSET = 16
NEAR_MIN_LEN = 3
NEAR_MAX_LEN = 4098  # 0x0FFF + 3

FAR_MIN_OFFSET = 17
FAR_MAX_OFFSET = 2064  # 0x07FF + 17
FAR_MIN_LEN = 3
FAR_MAX_LEN = 34  # 0x1F + 3


@dataclasses.dataclass(frozen=True)
class LZHeader:
    magic: bytes
    version: int
    header_size: int
    flags: int
    uncompressed_size: int
    compressed_size: int


def is_jdlz(data: bytes) -> bool:
    """Return True if `data` starts with a valid JDLZ v2 header."""
    return (
        len(data) >= JDLZ_HEADER_SIZE
        and data[:4] == JDLZ_MAGIC
        and data[4] == JDLZ_VERSION
    )


def parse_lz_header(data: bytes) -> LZHeader:
    """Parse a 16-byte EA Black Box LZHeader (`JDLZ`, `HUFF`, `RAWW`, `COMP`)."""
    if len(data) < JDLZ_HEADER_SIZE:
        raise ValueError(f"Buffer too small for LZHeader ({len(data)} < {JDLZ_HEADER_SIZE})")
    magic = data[:4]
    version, header_size, flags, uncompressed_size, compressed_size = struct.unpack_from(
        "<BBHII", data, 4
    )
    return LZHeader(
        magic=magic,
        version=version,
        header_size=header_size,
        flags=flags,
        uncompressed_size=uncompressed_size,
        compressed_size=compressed_size,
    )


def jdlz_decompress(src: bytes) -> bytes:
    """Decompress an EA Black Box JDLZ v2 stream.

    Mirrors the retail PS2 `SLUS_213.51` `JLZDecompress` loop faithfully,
    including handling empty payloads, overlapping RLE copies, simultaneous
    control/runtype refills, and EA's trailing 0x00 pad byte.
    """
    hdr = parse_lz_header(src)
    if hdr.magic != JDLZ_MAGIC:
        raise ValueError(f"Expected JDLZ magic, got {hdr.magic!r}")
    if hdr.version != JDLZ_VERSION:
        raise ValueError(f"Unsupported JDLZ version: {hdr.version} (expected {JDLZ_VERSION})")
    if hdr.header_size < JDLZ_HEADER_SIZE:
        raise ValueError(f"Invalid JDLZ header_size: {hdr.header_size}")
    if hdr.compressed_size > len(src):
        raise ValueError(
            f"Truncated JDLZ stream: header compressed_size={hdr.compressed_size} > len={len(src)}"
        )

    usize = hdr.uncompressed_size
    in_pos = hdr.header_size
    end_pos = hdr.compressed_size

    if usize == 0:
        return b""

    if in_pos + 2 > end_pos:
        raise ValueError(f"Truncated JDLZ stream before initial flag bytes (len={end_pos})")

    out = bytearray(usize)
    out_pos = 0

    control = src[in_pos] | 0x100
    runtype = src[in_pos + 1] | 0x100
    in_pos += 2
    remaining = end_pos - in_pos

    while remaining > 0:
        if control & 1:
            if remaining < 2:
                raise ValueError("Truncated JDLZ match token")
            b0 = src[in_pos]
            b1 = src[in_pos + 1]
            in_pos += 2
            remaining -= 2

            if runtype & 1:
                run = (((b0 >> 4) << 8) | b1) + 3
                off = (b0 & 0x0F) + 1
            else:
                off = (((b0 >> 5) << 8) | b1) + 17
                run = (b0 & 0x1F) + 3

            if off > out_pos:
                raise ValueError(
                    f"Corrupt JDLZ back-reference: offset {off} exceeds decoded length {out_pos}"
                )
            if out_pos + run > usize:
                raise ValueError(
                    f"Corrupt JDLZ stream: match write ({out_pos + run}) exceeds uncompressed_size ({usize})"
                )

            if off >= run:
                src_start = out_pos - off
                out[out_pos : out_pos + run] = out[src_start : src_start + run]
                out_pos += run
            elif off == 1:
                out[out_pos : out_pos + run] = bytes([out[out_pos - 1]]) * run
                out_pos += run
            else:
                # Overlapping copy (period = off): double-copy via slice tiling
                pattern = bytes(out[out_pos - off : out_pos])
                full, rem = divmod(run, off)
                chunk = pattern * full + pattern[:rem]
                out[out_pos : out_pos + run] = chunk
                out_pos += run

            runtype >>= 1
        else:
            remaining -= 1
            if out_pos < usize:
                out[out_pos] = src[in_pos]
                out_pos += 1
                in_pos += 1
            else:
                # Trailing pad byte after output is already complete (matches retail JLZDecompress)
                in_pos += 1

        control >>= 1
        if control == 1:
            if remaining <= 0:
                if out_pos == usize:
                    break
                raise ValueError("Truncated JDLZ stream while reloading control flags")
            control = src[in_pos] | 0x100
            in_pos += 1
            remaining -= 1

        if runtype == 1:
            if remaining <= 0:
                if out_pos == usize:
                    break
                raise ValueError("Truncated JDLZ stream while reloading runtype flags")
            runtype = src[in_pos] | 0x100
            in_pos += 1
            remaining -= 1

    if out_pos != usize:
        raise ValueError(
            f"Incomplete JDLZ decompression: decoded {out_pos} bytes, expected {usize}"
        )

    return bytes(out)


def _match_length(data: bytes, pos_a: int, pos_b: int, max_len: int) -> int:
    """Compute common prefix length starting at pos_a and pos_b, supporting overlap."""
    length = 0
    # Fast chunked comparison when non-overlapping
    if pos_b - pos_a >= 8:
        while length + 8 <= max_len and data[pos_a + length : pos_a + length + 8] == data[pos_b + length : pos_b + length + 8]:
            length += 8
    while length < max_len and data[pos_a + length] == data[pos_b + length]:
        length += 1
    return length


def jdlz_compress(data: bytes, max_chain: int = 32, hash_bits: int = 15) -> bytes:
    """Compress raw bytes into a valid EA Black Box JDLZ v2 stream.

    The produced bitstream strictly satisfies `SLUS_213.51`'s retail
    `JLZDecompress` loop invariant: every `control == 1` and `runtype == 1`
    reload has a corresponding byte present so `remaining` terminates at 0
    without underflow.

    Args:
        data: Uncompressed payload bytes.
        max_chain: Maximum hash-chain candidates to inspect per position (tradeoff
            between compression ratio and speed; default 32 is fast & effective).
        hash_bits: Hash table size exponent (2**hash_bits buckets).
    """
    n = len(data)
    if n == 0:
        # 16-byte header + 2 initial flag bytes -> remaining = 0 -> immediate clean return
        return struct.pack("<4sBBHII", JDLZ_MAGIC, JDLZ_VERSION, JDLZ_HEADER_SIZE, 0, 0, 18) + b"\x00\x00"

    out = bytearray(JDLZ_HEADER_SIZE + 2)
    flags1_pos = JDLZ_HEADER_SIZE
    flags2_pos = JDLZ_HEADER_SIZE + 1
    flags1 = 0
    flags2 = 0
    bits1 = 0
    bits2 = 0

    hash_mask = (1 << hash_bits) - 1
    head: Dict[int, int] = {}
    # Ring buffer of previous positions within the sliding window
    win_size = FAR_MAX_OFFSET + 1
    prev = [-1] * win_size

    def insert_pos(pos: int) -> None:
        if pos + 2 < n:
            h = ((data[pos] << 10) ^ (data[pos + 1] << 5) ^ data[pos + 2]) & hash_mask
            prev[pos % win_size] = head.get(h, -1)
            head[h] = pos

    i = 0
    while i < n:
        best_len = 0
        best_off = 0
        best_is_near = False

        if i + 3 <= n:
            # 1) Check near offsets (1..16) first because they allow up to 4098-byte RLE runs!
            max_near = min(NEAR_MAX_LEN, n - i)
            near_limit = min(NEAR_MAX_OFFSET, i)
            b0_cur = data[i]
            b1_cur = data[i + 1]
            b2_cur = data[i + 2]

            # Quick RLE check for offset == 1 (very common for zero-padded PS2 structs/chunks)
            if near_limit >= 1 and data[i - 1] == b0_cur and b1_cur == b0_cur and b2_cur == b0_cur:
                r = 3
                while r < max_near and data[i + r] == b0_cur:
                    r += 1
                best_len = r
                best_off = 1
                best_is_near = True

            # Check remaining near offsets 2..16 if we haven't already hit a huge RLE match
            if best_len < max_near and best_len < 256:
                for off in range(2, near_limit + 1):
                    p = i - off
                    if data[p] == b0_cur and data[p + 1] == b1_cur and data[p + 2] == b2_cur:
                        mlen = 3 + _match_length(data, p + 3, i + 3, max_near - 3)
                        if mlen > best_len:
                            best_len = mlen
                            best_off = off
                            best_is_near = True
                            if best_len == max_near:
                                break

            # 2) Search far offsets (17..2064, max len 34) via 3-byte hash chain
            h = ((b0_cur << 10) ^ (b1_cur << 5) ^ b2_cur) & hash_mask
            if best_len < FAR_MAX_LEN:
                max_far = min(FAR_MAX_LEN, n - i)
                min_pos = max(0, i - FAR_MAX_OFFSET)
                cand = head.get(h, -1)
                steps = 0
                while cand >= min_pos and steps < max_chain:
                    off = i - cand
                    if off >= FAR_MIN_OFFSET:
                        if (
                            data[cand] == b0_cur
                            and data[cand + 1] == b1_cur
                            and data[cand + 2] == b2_cur
                        ):
                            mlen = 3 + _match_length(data, cand + 3, i + 3, max_far - 3)
                            if mlen > best_len:
                                best_len = mlen
                                best_off = off
                                best_is_near = False
                                if best_len == max_far:
                                    break
                    next_cand = prev[cand % win_size]
                    if next_cand >= cand:
                        break
                    cand = next_cand
                    steps += 1

        if best_len >= 3:
            # Emit match token
            flags1 |= 1 << bits1
            bits1 += 1

            if best_is_near:
                flags2 |= 1 << bits2
                run_enc = best_len - 3
                off_enc = best_off - 1
                out.append(((run_enc >> 8) << 4) | off_enc)
                out.append(run_enc & 0xFF)
            else:
                run_enc = best_len - 3
                off_enc = best_off - 17
                out.append(((off_enc >> 8) << 5) | run_enc)
                out.append(off_enc & 0xFF)
            bits2 += 1

            # Update hash chain for matched bytes (cap updates on huge RLE runs for speed)
            end_i = i + best_len
            if best_len <= 64:
                for pos in range(i, end_i):
                    insert_pos(pos)
            else:
                insert_pos(i)
                insert_pos(end_i - 2)
                insert_pos(end_i - 1)
            i = end_i
        else:
            # Emit literal byte
            out.append(data[i])
            bits1 += 1
            insert_pos(i)
            i += 1

        # Reload flags in EXACT retail order: flags1 (control) FIRST, flags2 (runtype) SECOND!
        # Always allocate the reload byte immediately (even at end of input) so retail
        # `JLZDecompress` never underflows `remaining` when control==1 or runtype==1.
        if bits1 == 8:
            out[flags1_pos] = flags1
            flags1_pos = len(out)
            out.append(0)
            flags1 = 0
            bits1 = 0

        if bits2 == 8:
            out[flags2_pos] = flags2
            flags2_pos = len(out)
            out.append(0)
            flags2 = 0
            bits2 = 0

    # Flush final partial flag bytes
    out[flags1_pos] = flags1
    out[flags2_pos] = flags2

    # Write 16-byte JDLZ v2 header
    struct.pack_into(
        "<4sBBHII",
        out,
        0,
        JDLZ_MAGIC,
        JDLZ_VERSION,
        JDLZ_HEADER_SIZE,
        0,
        n,
        len(out),
    )
    return bytes(out)


def raww_wrap(data: bytes) -> bytes:
    """Wrap uncompressed bytes in an EA `RAWW` 16-byte stored LZHeader."""
    n = len(data)
    return struct.pack("<4sBBHII", RAWW_MAGIC, 1, JDLZ_HEADER_SIZE, 0, n, n + JDLZ_HEADER_SIZE) + data


def lz_decompress(data: bytes) -> bytes:
    """Decompress `JDLZ` or unwrap `RAWW` streams automatically."""
    hdr = parse_lz_header(data)
    if hdr.magic == JDLZ_MAGIC:
        return jdlz_decompress(data)
    if hdr.magic == RAWW_MAGIC:
        start = hdr.header_size
        end = start + hdr.uncompressed_size
        if end > len(data):
            raise ValueError("Truncated RAWW payload")
        return data[start:end]
    raise ValueError(f"Unsupported EA LZ magic: {hdr.magic!r}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="EA Black Box JDLZ v2 decompressor, compressor, and header inspector"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_info = sub.add_parser("info", help="Inspect a JDLZ/RAWW file header")
    p_info.add_argument("input", type=Path)

    p_dec = sub.add_parser("decompress", help="Decompress a JDLZ (or RAWW) file")
    p_dec.add_argument("input", type=Path)
    p_dec.add_argument("-o", "--output", type=Path, required=True)

    p_cmp = sub.add_parser("compress", help="Compress a raw file into JDLZ v2")
    p_cmp.add_argument("input", type=Path)
    p_cmp.add_argument("-o", "--output", type=Path, required=True)
    p_cmp.add_argument("--max-chain", type=int, default=32, help="Hash chain search depth (default: 32)")

    args = parser.parse_args()
    raw = args.input.read_bytes()

    if args.cmd == "info":
        hdr = parse_lz_header(raw)
        ratio = (hdr.compressed_size / hdr.uncompressed_size * 100.0) if hdr.uncompressed_size else 0.0
        print(f"Magic             : {hdr.magic.decode('ascii', errors='replace')}")
        print(f"Version           : {hdr.version}")
        print(f"Header Size       : {hdr.header_size} bytes")
        print(f"Flags             : 0x{hdr.flags:04X}")
        print(f"Uncompressed Size : {hdr.uncompressed_size:,} bytes")
        print(f"Compressed Size   : {hdr.compressed_size:,} bytes ({ratio:.1f}%)")
        return 0

    if args.cmd == "decompress":
        dec = lz_decompress(raw)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(dec)
        print(f"Decompressed {args.input} ({len(raw):,} B) -> {args.output} ({len(dec):,} B)")
        return 0

    if args.cmd == "compress":
        cmp_data = jdlz_compress(raw, max_chain=args.max_chain)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(cmp_data)
        ratio = (len(cmp_data) / len(raw) * 100.0) if raw else 0.0
        print(f"Compressed {args.input} ({len(raw):,} B) -> {args.output} ({len(cmp_data):,} B, {ratio:.1f}%)")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
