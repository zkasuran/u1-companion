#!/bin/sh
# Prepare the directories Moonraker expects, then run it from the mounted
# source. Nothing here patches the fork.
set -eu

: "${MOONRAKER_SRC:=/opt/moonraker}"
: "${MOONRAKER_DATA_PATH:=/data}"
: "${MOONRAKER_CONFIG_PATH:=/config/moonraker.conf}"
: "${MOONRAKER_TMP_DIR:=/tmp/moonraker}"

if [ ! -f "$MOONRAKER_SRC/moonraker/server.py" ]; then
    echo "no Moonraker source at $MOONRAKER_SRC" >&2
    echo "clone https://github.com/Snapmaker/U1-Moonraker and mount it there," >&2
    echo "see docker-compose.yml" >&2
    exit 1
fi

# server.py creates the data path itself, plus comms and misc. These it opens
# files in without creating first. Each missing one is either a hard startup
# failure or a component that fails to load:
#   <data>/config/snapmaker  machine.py:81, :1059    product_info.json
#   <data>/mqtt              client_manager.py:917   client.json
#   the tmp dir              server.py:679           defaults to /userdata/.tmp
# logs and gcodes are ours to create because [file_manager] registers them.
mkdir -p "$MOONRAKER_DATA_PATH/logs" \
         "$MOONRAKER_DATA_PATH/gcodes" \
         "$MOONRAKER_DATA_PATH/database" \
         "$MOONRAKER_DATA_PATH/comms" \
         "$MOONRAKER_DATA_PATH/config/snapmaker" \
         "$MOONRAKER_DATA_PATH/mqtt" \
         "$MOONRAKER_TMP_DIR"

echo "u1-companion: starting Moonraker from $MOONRAKER_SRC"
cd "$MOONRAKER_SRC"
exec python -m moonraker \
    -d "$MOONRAKER_DATA_PATH" \
    -c "$MOONRAKER_CONFIG_PATH" \
    -t "$MOONRAKER_TMP_DIR" \
    -n \
    "$@"
