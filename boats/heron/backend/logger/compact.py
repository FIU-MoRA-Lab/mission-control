"""Compact closed RRDs, replacing them only after checking every stored value."""
import hashlib
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

import pyarrow as pa
from rerun.chunk import RrdReader

from boats.heron.paths import publish_file


def fingerprint(path):
    """Order/chunk-independent digest with bounded memory, including exact clocks.

    Compaction can merge sparse columns and split rows between chunks. Hash each
    present cell with its store, entity, row ID, name and type instead of hashing
    serialized chunks. The count and 256-bit sum retain repeated cells too.
    """
    count = total = 0
    stores = []
    reader = RrdReader(path)
    for store in reader.recordings():
        store_key = (store.kind, store.application_id, store.recording_id)
        stores.append(store_key)
        for chunk in reader.stream(store=store):
            batch = chunk.to_record_batch()
            row_ids = batch.column("rerun.controls.RowId").to_pylist()
            for field, column in zip(batch.schema, batch.columns):
                if field.name == "rerun.controls.RowId":
                    continue
                if pa.types.is_timestamp(field.type) or pa.types.is_duration(field.type):
                    column = column.cast(pa.int64())
                identity = (store_key, chunk.entity_path, field.name, str(field.type),
                            sorted((field.metadata or {}).items()))
                prefix = repr(identity).encode()
                for row_id, value in zip(row_ids, column.to_pylist()):
                    if value is not None:
                        digest = hashlib.sha256(prefix + repr((row_id, value)).encode()).digest()
                        total += int.from_bytes(digest, "big")
                        count += 1
    return sorted(stores), count, total % (1 << 256)


def compact_rrd(path):
    """Return whether a verified smaller file replaced the original.

    The original stays published throughout optimization/verification. Temporary
    files live beside it so publication is atomic on the same filesystem.
    Exceptions leave the original untouched and are handled by the caller.
    """
    path = Path(path).resolve()
    with tempfile.TemporaryDirectory(prefix=".rrd-compact-", dir=path.parent) as tmp:
        candidate = Path(tmp) / path.name
        result = subprocess.run(
            [sys.executable, "-m", "rerun_cli", "rrd", "optimize", str(path), "-o", str(candidate)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            raise RuntimeError(f"RRD optimization failed: {result.stderr[-2000:]}")
        if candidate.stat().st_size >= path.stat().st_size:
            return False
        if fingerprint(path) != fingerprint(candidate):
            raise ValueError("RRD compaction changed stored values, types or timelines")
        before = path.stat().st_size
        after = candidate.stat().st_size
        publish_file(candidate, path)
        logging.getLogger(__name__).info("Compacted %s: %d → %d bytes", path, before, after)
        return True


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.paths:
        before = path.stat().st_size
        compact_rrd(path)
        print(f"{path}: {before:,} → {path.stat().st_size:,} bytes")
