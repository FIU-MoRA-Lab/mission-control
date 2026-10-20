#!/bin/sh
# Keep this terminal open while viewing the USB camera. Ctrl-C stops the source.
set -eu
exec ssh -t -- "${1:-pi@192.168.2.2}" \
  "docker exec -it blueos-core gst-launch-1.0 -e \
    v4l2src device=/dev/video0 ! \
    video/x-raw,width=640,height=480,framerate=10/1 ! \
    videoconvert ! video/x-raw,format=I420 ! \
    x264enc tune=zerolatency speed-preset=ultrafast bitrate=1500 key-int-max=10 ! \
    h264parse ! matroskamux streamable=true ! \
    tcpserversink host=0.0.0.0 port=5601 sync=false"
