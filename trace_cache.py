"""Local compact cache for parsed rtldump traces.

The cache is keyed by input file path, size, and mtime. It stores only the
fields used by the timing model and benchmark-window detector, avoiding the
large raw commit-line strings from the RTL dump.
"""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
import struct
import tempfile
from typing import BinaryIO, Sequence

from trace import CSRWrite, RegWrite, TraceEntry, _annotate_next_pcs, parse_trace_files


SCHEMA_VERSION = 2
MAGIC = b"SHKTTRC2"
HEADER_LEN = struct.Struct("<I")
RECORD = struct.Struct("<QIqBBBB")
REG_WRITE = struct.Struct("<BBQ")
CSR_WRITE = struct.Struct("<HQ")
MEM_ADDR = struct.Struct("<Q")
NO_CYCLE = -(1 << 63)


def load_or_parse_trace_files(
    paths: Sequence[str | Path],
    *,
    cache_dir: str | Path = ".trace-cache",
    limit: int | None = None,
) -> list[TraceEntry]:
    """Load a parsed trace from cache, or parse and cache it."""

    if limit is not None:
        return parse_trace_files(paths, limit=limit)

    cache_root = Path(cache_dir)
    cache_root.mkdir(parents=True, exist_ok=True)
    key = _cache_key(paths)
    cache_path = cache_root / f"{key}.bin"
    if cache_path.exists():
        return _read_cache(cache_path, key)

    entries = parse_trace_files(paths)
    _write_cache(cache_path, key, entries)
    return entries


def _cache_key(paths: Sequence[str | Path]) -> str:
    signature = []
    for path_like in paths:
        path = Path(path_like).resolve()
        stat = path.stat()
        signature.append(
            {
                "path": str(path),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    blob = json.dumps({"schema": SCHEMA_VERSION, "files": signature}, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _write_cache(path: Path, key: str, entries: list[TraceEntry]) -> None:
    payload = {"schema": SCHEMA_VERSION, "key": key, "count": len(entries)}
    header = json.dumps(payload, sort_keys=True).encode("utf-8")
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
        handle.write(MAGIC)
        handle.write(HEADER_LEN.pack(len(header)))
        handle.write(header)
        for entry in entries:
            _write_entry(handle, entry)
        tmp_name = handle.name
    Path(tmp_name).replace(path)


def _read_cache(path: Path, key: str) -> list[TraceEntry]:
    with path.open("rb") as handle:
        if handle.read(len(MAGIC)) != MAGIC:
            raise ValueError(f"unsupported trace cache format: {path}")
        header_len = HEADER_LEN.unpack(_read_exact(handle, HEADER_LEN.size))[0]
        header = json.loads(_read_exact(handle, header_len).decode("utf-8"))
        if header.get("schema") != SCHEMA_VERSION or header.get("key") != key:
            raise ValueError(f"stale trace cache metadata: {path}")
        entries = [_read_entry(handle, idx) for idx in range(int(header["count"]))]
    _annotate_next_pcs(entries)
    return entries


def _write_entry(handle: BinaryIO, entry: TraceEntry) -> None:
    if len(entry.reg_writes) > 255 or len(entry.csr_writes) > 255 or len(entry.mem_addresses) > 255:
        raise ValueError("trace cache record count exceeds compact format")
    mode = int(entry.mode, 0) if entry.mode.isdigit() else 255
    cycle = entry.cycle if entry.cycle is not None else NO_CYCLE
    handle.write(
        RECORD.pack(
            entry.pc,
            entry.encoding,
            cycle,
            mode,
            len(entry.reg_writes),
            len(entry.csr_writes),
            len(entry.mem_addresses),
        )
    )
    for write in entry.reg_writes:
        handle.write(REG_WRITE.pack(0 if write.kind == "x" else 1, write.reg, write.value))
    for write in entry.csr_writes:
        handle.write(CSR_WRITE.pack(write.number, write.value))
    for address in entry.mem_addresses:
        handle.write(MEM_ADDR.pack(address))


def _read_entry(handle: BinaryIO, index: int) -> TraceEntry:
    pc, encoding, cycle, mode, reg_count, csr_count, mem_count = RECORD.unpack(_read_exact(handle, RECORD.size))
    entry = TraceEntry(
        index=index,
        pc=pc,
        encoding=encoding,
        mode=str(mode) if mode != 255 else "",
        cycle=None if cycle == NO_CYCLE else cycle,
    )
    for _ in range(reg_count):
        kind, reg, value = REG_WRITE.unpack(_read_exact(handle, REG_WRITE.size))
        entry.reg_writes.append(RegWrite("x" if kind == 0 else "f", reg, value))
    for _ in range(csr_count):
        number, value = CSR_WRITE.unpack(_read_exact(handle, CSR_WRITE.size))
        entry.csr_writes.append(CSRWrite(number, "", value))
    for _ in range(mem_count):
        (address,) = MEM_ADDR.unpack(_read_exact(handle, MEM_ADDR.size))
        entry.mem_addresses.append(address)
    return entry


def _read_exact(handle: BinaryIO, size: int) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise EOFError("truncated trace cache")
    return data
