"""Install/manage the onboard camera encoder; also its stdlib-only service entry point."""
import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys

SERVICE = "mission-control-camera.service"
CONTAINER = "mission-control-camera"
CONFIG = "/etc/mission-control/camera.json"

# Runs once on the Pi through SSH. No background SSH process is kept.
INSTALL = '''
import json, os, pathlib, subprocess, sys
payload = json.load(sys.stdin)
files = {
    "/usr/local/lib/mission-control/camera_service.py": payload["program"],
    "/etc/systemd/system/mission-control-camera.service": payload["unit"],
    "/etc/mission-control/camera.json": json.dumps(payload["config"], indent=2) + "\\n",
}
for name, contents in files.items():
    path = pathlib.Path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(contents)
    os.chmod(temporary, 0o644)
    temporary.replace(path)
subprocess.run(["systemctl", "daemon-reload"], check=True)
subprocess.run(["systemctl", "enable", "mission-control-camera.service"], check=True)
subprocess.run(["systemctl", "restart", "mission-control-camera.service"], check=True)
subprocess.run(["systemctl", "status", "mission-control-camera.service", "--no-pager"], check=True)
'''


def configuration(device="/dev/video0", container="blueos-core", port=5601, fps=30):
    if not isinstance(device, str) or not device.startswith("/dev/") or any(c in device for c in "\n\r\0:"):
        raise ValueError("Choose a camera path under /dev (for example /dev/video0)")
    if not isinstance(container, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", container):
        raise ValueError("Choose a valid BlueOS container name")
    if container == CONTAINER:
        raise ValueError("The source image container must be different from the camera service")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Choose a camera port between 1 and 65535")
    if type(fps) is not int or not 1 <= fps <= 60:
        raise ValueError("Choose a camera frame rate between 1 and 60 fps")
    return dict(device=device, container=container, port=port, fps=fps)


def encoder_command(config, image):
    """Own container, installed BlueOS image, no Docker socket or vehicle devices."""
    return [
        "/usr/bin/docker", "run", "--rm", "--name", CONTAINER,
        "--network", "host", "--device", f"{config['device']}:/dev/video0:rw",
        "--user", "0:0", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", "/tmp:rw,size=16m", "--env", "GST_REGISTRY=/tmp/gstreamer-registry.bin",
        "--log-driver", "local", "--log-opt", "max-size=5m", "--log-opt", "max-file=2",
        "--entrypoint", "gst-launch-1.0", image, "-e",
        "v4l2src", "device=/dev/video0", "do-timestamp=true", "!",
        f"video/x-raw,width=640,height=480,framerate={config['fps']}/1", "!",
        "videoconvert", "!", "video/x-raw,format=I420", "!",
        "x264enc", "tune=zerolatency", "speed-preset=ultrafast", "bitrate=3000", f"key-int-max={config['fps']}", "!",
        "h264parse", "config-interval=-1", "!", "matroskamux", "streamable=true", "!",
        "tcpserversink", "host=0.0.0.0", f"port={config['port']}", "sync=false",
    ]


def serve(path=CONFIG):
    config = configuration(**json.loads(Path(path).read_text()))
    # Re-resolve after restarts so BlueOS upgrades do not leave a stale image pin.
    image = subprocess.run([
        "/usr/bin/docker", "inspect", "--type", "container", "--format", "{{.Image}}", config["container"],
    ], check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise RuntimeError("Cannot identify the installed BlueOS image")
    print(f"Camera {config['device']} → H.264 {config['fps']} fps TCP :{config['port']}", flush=True)
    return subprocess.run(encoder_command(config, image)).returncode


def install_payload(config):
    return json.dumps(dict(program=Path(__file__).read_text(),
                           unit=Path(__file__).with_name("camera.service").read_text(),
                           config=config)).encode()


def ssh_command(host, command):
    if not host or host.startswith("-") or any(c.isspace() for c in host):
        raise ValueError("Choose an SSH destination such as pi@192.168.2.2")
    return ["ssh", "-o", "ConnectTimeout=5", "--", host, command]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "status", "logs", "restart", "stop", "serve"])
    parser.add_argument("--host", default="pi@192.168.2.2", help="SSH destination for one-time setup/administration")
    parser.add_argument("--device", default="/dev/video0", help="USB camera path on the Pi; install only")
    parser.add_argument("--container", default="blueos-core", help="Installed BlueOS image source; install only")
    parser.add_argument("--port", type=int, default=5601, help="Video TCP port; install only")
    parser.add_argument("--fps", type=int, default=30, help="Capture frame rate; install only (camera must support it)")
    parser.add_argument("--config", default=CONFIG, help="Onboard configuration file; serve only")
    args = parser.parse_args()
    try:
        if args.action == "serve":
            return serve(args.config)
        if args.action == "install":
            config = configuration(args.device, args.container, args.port, args.fps)
            command = "sudo -n /usr/bin/python3 -c " + shlex.quote(INSTALL)
            result = subprocess.run(ssh_command(args.host, command), input=install_payload(config))
            if result.returncode == 0:
                print(f"Camera service installed and enabled at boot (TCP port {args.port}).")
            return result.returncode
        if args.action == "logs":
            command = f"sudo -n journalctl -u {SERVICE} -n 100 --no-pager"
        else:
            command = f"sudo -n systemctl {args.action} {SERVICE} --no-pager"
        return subprocess.run(ssh_command(args.host, command)).returncode
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Camera service: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
