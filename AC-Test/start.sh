#!/bin/bash
# ACSwitcher test-server launcher: runs PandaSpigot with a named-pipe console.
# Usage: ./start.sh          (starts server, console at /tmp/ac-console)
#        echo "cmd" > /tmp/ac-console   (send a command)
cd "$(dirname "$0")"
PIPE=/tmp/ac-console
rm -f "$PIPE"
mkfifo "$PIPE"
# keep the pipe open so java's stdin never EOFs
( exec 3>"$PIPE"; sleep 100000 ) &
tail -f "$PIPE" | /home/zoroaster/.sdkman/candidates/java/17.0.19.fx-zulu/bin/java -Xmx2G -Xms1G -jar server.jar nogui
