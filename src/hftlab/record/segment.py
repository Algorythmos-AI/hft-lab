"""One segment file: written by appending whole blocks, read back with its torn tail recognised."""
from __future__ import annotations

import gzip
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from hftlab.record.format import BlockScan, CorruptSegment, Header, Record, pack_block, read_blocks, read_header


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


class SegmentWriter:
    """Appends blocks to one segment. A new file gets a header; an existing one is cut back to its last good block."""

    def __init__(self, path: Path, header: Header, *, fsync: bool = True) -> None:
        self.path = path
        self._fsync = fsync
        self._buffer: list[Record] = []
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
            path.parent.mkdir(parents=True, exist_ok=True)
            self.header = header
            self._fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL, 0o640)
            packed = header.pack()
            self._write(packed)
            self._sync()
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
        """Write everything buffered as one block, in one append. Returns the bytes added."""
        if not self._buffer:
            return 0
        block = pack_block(self._buffer)
        self._write(block)
        self._sync()
        self.last = self._buffer[-1]
        self._buffer = []
        self.size += len(block)
        return len(block)

    def discard(self) -> int:
        """Drop what is buffered without writing it. Returns how many records were dropped."""
        dropped, self._buffer = len(self._buffer), []
        return dropped

    def close(self) -> None:
        if self._fd >= 0:
            self.flush()
            os.close(self._fd)
            self._fd = -1

    def _write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(self._fd, view) :]

    def _sync(self) -> None:
        if self._fsync:
            os.fsync(self._fd)
