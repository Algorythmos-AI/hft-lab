"""One segment file: written by appending whole blocks, read back with its torn tail recognised."""
from __future__ import annotations

import contextlib
import errno
import gzip
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from hftlab.record.format import (
    BLOCK_HEAD,
    BLOCK_MAGIC,
    MAX_BLOCK_RECORDS,
    RECORD_SIZE,
    BlockScan,
    CorruptSegment,
    Header,
    Record,
    pack_block,
    read_blocks,
    read_header,
)


def _open(path: Path) -> IO[bytes]:
    return gzip.open(path, "rb") if path.suffix == ".gz" else path.open("rb")  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class SegmentInfo:
    header: Header
    header_bytes: int
    scan: BlockScan
    last: Record | None

    @property
    def good_end(self) -> int:
        """The byte offset where the last whole block ends: where a writer may safely continue."""
        return self.header_bytes + self.scan.good_bytes


def read_segment(path: Path) -> tuple[Header, BlockScan, Iterator[Record]]:
    """(header, scan, records). `scan` is complete only once the records have been read to the end."""
    stream = _open(path)
    try:
        header = read_header(stream)
    except BaseException:
        stream.close()
        raise
    scan = BlockScan()

    def records() -> Iterator[Record]:
        with stream:
            for block in read_blocks(stream, scan):
                yield from block

    return header, scan, records()


def inspect(path: Path) -> SegmentInfo:
    """Read a whole segment for its header, its last record and where its good data ends."""
    with _open(path) as stream:
        header = read_header(stream)
        header_bytes = stream.tell()
        scan, last = BlockScan(), None
        for block in read_blocks(stream, scan):
            last = block[-1]
    return SegmentInfo(header, header_bytes, scan, last)


def count_records(path: Path) -> int:
    """How many records a segment holds, from its block heads alone. Nothing is unpacked and nothing is checked.

    It is for a status screen, which wants a number often and cheaply. Evidence comes from `inspect` and the
    day check, which verify every checksum. A block that is not whole, or is not a block, ends the count.
    """
    total = 0
    with _open(path) as stream:
        read_header(stream)
        while len(head := stream.read(BLOCK_HEAD.size)) == BLOCK_HEAD.size:
            magic, count, _ = BLOCK_HEAD.unpack(head)
            if magic != BLOCK_MAGIC or not 0 < count <= MAX_BLOCK_RECORDS:
                break
            if len(stream.read(count * RECORD_SIZE)) < count * RECORD_SIZE:
                break
            total += count
    return total


class SegmentWriter:
    """Appends blocks to one segment. A new file gets a header; an existing one is cut back to its last good block."""

    def __init__(self, path: Path, header: Header, *, fsync: bool = True) -> None:
        self.path = path
        self._fsync = fsync
        self._buffer: list[Record] = []
        self._failed = False
        self.last: Record | None = None
        if path.exists():
            info = inspect(path)
            if info.header.pairs != header.pairs:
                raise CorruptSegment(f"{path} was written for other pairs and cannot be continued")
            self.header, self.last = info.header, info.last
            self._fd = os.open(path, os.O_WRONLY | os.O_APPEND)
            if os.fstat(self._fd).st_size != info.good_end:
                os.ftruncate(self._fd, info.good_end)  # the torn tail of a killed writer: never appended after
                self._sync()
            self.size = info.good_end
        else:
            # A segment appears with its header whole or not at all. A header cut short by a kill or by a full
            # disk would be a file that nothing can read and nothing may repair, and the store could not be
            # opened again. So the header is written beside the segment and moved into place in one step.
            path.parent.mkdir(parents=True, exist_ok=True)
            self.header = header
            packed = header.pack()
            partial = path.with_name(path.name + ".partial")
            self._fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
            try:
                self._write(packed)
                self._sync()
            finally:
                fd, self._fd = self._fd, -1
                os.close(fd)
            os.replace(partial, path)
            self._fd = os.open(path, os.O_WRONLY | os.O_APPEND)
            self.size = len(packed)

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    def append(self, record: Record) -> None:
        self._buffer.append(record)

    def pending_bytes(self) -> int:
        """How many bytes the next `flush` would add."""
        return len(pack_block(self._buffer)) if self._buffer else 0

    def flush(self) -> int:
        """Write everything buffered as one block, in one append. Returns the bytes added.

        A write or a sync the operating system refuses ends this writer. The piece of a block that got out is
        taken back if the disk allows it, and nothing is appended here again: a block written after a torn one
        would make the torn one read as corruption. The next writer of this file is a new one, which cuts the
        tail and continues from the last whole block.
        """
        if not self._buffer:
            return 0
        if self._failed:
            raise OSError(errno.EIO, "an earlier write to this segment failed; it is not appended to again")
        block = pack_block(self._buffer)
        try:
            self._write(block)
            self._sync()
        except OSError:
            self._failed = True
            with contextlib.suppress(OSError):
                os.ftruncate(self._fd, self.size)
            raise
        self.last = self._buffer[-1]
        self._buffer = []
        self.size += len(block)
        return len(block)

    def discard(self) -> int:
        """Drop what is buffered without writing it. Returns how many records were dropped."""
        dropped, self._buffer = len(self._buffer), []
        return dropped

    def close(self) -> None:
        """Flush and close. A writer that failed closes without writing, and the file is closed either way."""
        if self._fd >= 0:
            try:
                if not self._failed:
                    self.flush()
            finally:
                fd, self._fd = self._fd, -1
                os.close(fd)

    def _write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(self._fd, view) :]

    def _sync(self) -> None:
        if self._fsync:
            os.fsync(self._fd)
