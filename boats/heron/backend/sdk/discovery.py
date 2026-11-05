"""Private loopback connection discovery, scoped to this checkout/data directory."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
from boats.heron.paths import data_directory


def endpoint_path():
    return Path(os.environ.get("MISSION_CONTROL_ENDPOINT_FILE",data_directory()/".mission-control"/"connection.json"))


@contextmanager
def publish_endpoint(port, token):
    path = endpoint_path()
    path.parent.mkdir(parents=True,exist_ok=True)
    value = {"url":f"http://127.0.0.1:{port}","token":token,"pid":os.getpid()}
    temporary = path.with_name(f"connection-{os.getpid()}.tmp")
    fd = os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,"w") as stream:
        json.dump(value,stream)
    os.replace(temporary,path)
    try:
        yield path
    finally:
        try:
            if json.loads(path.read_text()).get("token") == token:
                path.unlink()
        except (FileNotFoundError,ValueError):
            pass
