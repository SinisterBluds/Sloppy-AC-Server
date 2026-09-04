#!/bin/bash
# AC Test Server launcher (PandaSpigot 1.8.8 on Java 17 - required for GrimAC)
cd "$(dirname "$0")"
exec /home/zoroaster/.sdkman/candidates/java/17.0.19.fx-zulu/bin/java -Xmx2G -Xms1G -jar server.jar nogui
