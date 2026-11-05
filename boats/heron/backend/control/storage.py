"""Atomic run manifests and a durable index, independent of execution and transport."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from boats.heron.paths import data_directory


def save_json(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary = path.with_name(path.name+".tmp")
    with temporary.open("w") as stream:
        json.dump(value,stream,indent=2,allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary,path)


def stamp():
    return datetime.now(timezone.utc).isoformat()


class RunStore:
    def __init__(self):
        self.path = data_directory()/".mission-control"/"runs.json"
        self.index = json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self, manifest):
        save_json(Path(manifest["directory"])/"manifest.json",manifest)
        self.index[manifest["id"]] = manifest["directory"]
        save_json(self.path,self.index)

    def get(self, run_id):
        directory = self.index.get(run_id)
        if directory is None:
            raise ValueError("Unknown run")
        manifest = json.loads((Path(directory)/"manifest.json").read_text())
        if manifest["status"] in {"recording","finishing"}:
            try:
                os.kill(manifest["owner_pid"],0)
            except ProcessLookupError:
                manifest.update(status="interrupted",reason="Backend exited before finalization",ended_at=stamp())
                self.save(manifest)
            except PermissionError:
                pass
        return manifest

    def list(self):
        return [self.get(run_id) for run_id in reversed(self.index)]
