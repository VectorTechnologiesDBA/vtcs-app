#!/bin/bash
# Points a WireOS build tree at your own community server, so robots running the resulting CFW use it out of the
# box: voice/jdocs/token (wire-pod, port 443) and OS updates (the community server's /vic/, port 8443).
# Usage: point-cfw-at-server.sh [server-name] [wire-os dir]
set -e
SERVER="${1:-server.vectortech.dev}"
WIREOS="${2:-$HOME/wire-os}"
cd "$WIREOS/anki/victor"

for t in shipping beta development; do
	f="resources/$t/config/server_config.json"
	sed -i -E "s#\"(jdocs|tms|chipper)\": \"[^\"]*\"#\"\1\": \"$SERVER:443\"#; s#\"check\": \"[^\"]*\"#\"check\": \"$SERVER/ok\"#" "$f"
	echo "== $f"; cat "$f"; echo
done

# Development builds (./build/build.sh -bt dev) use the BASE_URL in the final else() branch.
sed -i -E "s#set\(BASE_URL \"https://[^\"]*/vic/\"\)#set(BASE_URL \"https://$SERVER:8443/vic/\")#; s#set\(BASE_URL_LATEST \"https://[^\"]*/vic/\"\)#set(BASE_URL_LATEST \"https://$SERVER:8443/vic/\")#" platform/config/CMakeLists.txt
echo "== update URLs in platform/config/CMakeLists.txt:"
grep -n "$SERVER" platform/config/CMakeLists.txt
git diff --stat -- resources platform/config
