#!/bin/bash
# Deploy the community server and the per-robot wire-pod patch on the server (Raspberry Pi), from a copy of this
# folder (e.g. ~/vpp-deploy).
#
#   bash deploy.sh server            install vpp_server.py, create wire-pod's per-bot folder, restart vpp-server
#   bash deploy.sh build-wirepod     build a patched wire-pod in ~/wire-pod-build (the running one isn't touched)
#   bash deploy.sh install-wirepod   swap it in for /usr/bin/wire-pod; puts the old one back if it doesn't start
#   bash deploy.sh rollback-wirepod  put the original /usr/bin/wire-pod back
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ME=$(id -un)
PERBOT=/etc/wire-pod/per-bot
BUILD=$HOME/wire-pod-build
VOSK_VER=0.3.45

say() { printf '\n== %s\n' "$*"; }

server() {
	local dir
	dir=$(systemctl cat vpp-server 2>/dev/null | sed -n 's/^WorkingDirectory=//p' | tail -n1)
	if [ -z "$dir" ]; then
		dir=$(systemctl cat vpp-server 2>/dev/null | sed -n 's/^ExecStart=.* \([^ ]*\)vpp_server\.py.*/\1/p' | tail -n1)
	fi
	[ -n "$dir" ] && [ -f "$dir/vpp_server.py" ] || { echo "Can't find the running vpp_server.py (systemctl cat vpp-server)."; exit 1; }
	say "Installing vpp_server.py into $dir"
	cp "$dir/vpp_server.py" "$dir/vpp_server.py.bak.$(date +%Y%m%d-%H%M%S)"
	cp "$HERE/vpp_server.py" "$dir/vpp_server.py"
	python3 -m py_compile "$dir/vpp_server.py"
	say "Per-robot settings folder $PERBOT (written by $ME, read by wire-pod)"
	sudo install -d -o "$ME" -m 755 "$PERBOT"
	say "Restarting vpp-server"
	sudo systemctl restart vpp-server
	sleep 3
	systemctl is-active vpp-server
	curl -sk https://127.0.0.1:8443/v1; echo
	echo "Done. Robots get claimed the next time their owner's app connects to them."
}

build_wirepod() {
	local arch goarch
	arch=$(uname -m)
	case "$arch" in
		aarch64) goarch=arm64 ;;
		armv7l) goarch=armv6l ;;
		x86_64) goarch=amd64 ;;
		*) echo "Unsupported CPU: $arch"; exit 1 ;;
	esac
	say "Build tools"
	sudo apt-get install -y -qq git unzip wget pkg-config libopus-dev libsodium-dev build-essential >/dev/null
	export PATH=/usr/local/go/bin:$PATH
	if ! command -v go >/dev/null; then
		say "Installing Go (to /usr/local/go)"
		wget -q -O /tmp/go.tgz "https://go.dev/dl/go1.22.12.linux-$goarch.tar.gz"
		sudo tar -C /usr/local -xzf /tmp/go.tgz
		rm /tmp/go.tgz
	fi
	go version
	if [ ! -f "$HOME/.vosk/libvosk/vosk_api.h" ]; then
		say "Vosk $VOSK_VER (speech to text library)"
		mkdir -p "$HOME/.vosk"
		cd "$HOME/.vosk"
		wget -q "https://github.com/alphacep/vosk-api/releases/download/v$VOSK_VER/vosk-linux-$arch-$VOSK_VER.zip"
		unzip -oq "vosk-linux-$arch-$VOSK_VER.zip"
		rm -rf libvosk
		mv "vosk-linux-$arch-$VOSK_VER" libvosk
		rm "vosk-linux-$arch-$VOSK_VER.zip"
	fi
	say "wire-pod source"
	rm -rf "$BUILD"
	git clone --depth 1 --progress https://github.com/kercre123/wire-pod.git "$BUILD"
	echo "installed wire-pod: $(cat /etc/wire-pod/version 2>/dev/null)   source: $(git -C "$BUILD" log -1 --format='%h %ad' --date=short)"
	say "Applying the per-robot patch"
	python3 "$HERE/wire-pod-patch/apply.py" "$BUILD"
	say "Building (takes a while on a Pi)"
	cd "$BUILD/chipper"
	export CGO_ENABLED=1 CGO_CFLAGS="-I$HOME/.vosk/libvosk" CGO_LDFLAGS="-L$HOME/.vosk/libvosk -lvosk -ldl -lpthread"
	go build -tags nolibopusfile \
		-ldflags "-X 'github.com/kercre123/wire-pod/chipper/pkg/vars.CommitSHA=$(git rev-parse --short HEAD)-perbot'" \
		-o "$BUILD/wire-pod" ./cmd/packaged/
	say "Built $BUILD/wire-pod"
	echo "Libraries it needs, and whether they're found:"
	LD_LIBRARY_PATH="$HOME/.vosk/libvosk" ldd "$BUILD/wire-pod" | grep -E "vosk|opus|not found" || true
	echo
	echo "Current /usr/bin/wire-pod uses:"
	ldd /usr/bin/wire-pod | grep -E "vosk|not found" || echo "  (no separate vosk library)"
	echo
	echo "Next: bash $0 install-wirepod"
}

install_wirepod() {
	[ -x "$BUILD/wire-pod" ] || { echo "Build first: bash $0 build-wirepod"; exit 1; }
	[ -f /usr/bin/wire-pod.orig ] || sudo cp -p /usr/bin/wire-pod /usr/bin/wire-pod.orig
	# The new build links against Vosk from ~/.vosk; give it a system-wide copy so the service finds it.
	if ! ldd /usr/bin/wire-pod.orig | grep -q "libvosk.*=> /"; then
		sudo install -m 644 "$HOME/.vosk/libvosk/libvosk.so" /usr/local/lib/libvosk.so
		sudo ldconfig
	fi
	say "Swapping in the patched wire-pod"
	sudo systemctl stop wire-pod
	sudo cp -p /etc/wire-pod/wire-pod/apiConfig.json /etc/wire-pod/wire-pod/apiConfig.json.before-perbot
	sudo install -m 755 "$BUILD/wire-pod" /usr/bin/wire-pod
	sudo systemctl start wire-pod
	# Up means the web page (8080) and the voice server Vector talks to (443, with its certificate) both answer.
	local ok=""
	for _ in $(seq 1 12); do
		sleep 5
		if systemctl is-active --quiet wire-pod && curl -s -o /dev/null http://127.0.0.1:8080/ &&
			echo | openssl s_client -connect 127.0.0.1:443 2>/dev/null | grep -q "BEGIN CERTIFICATE"; then
			ok=1
			break
		fi
	done
	if [ -n "$ok" ]; then
		echo "wire-pod is running the patched build."
		echo "Check that Vector still answers a voice command. If anything is off: bash $0 rollback-wirepod"
	else
		echo "The patched wire-pod didn't come up; putting the original back."
		sudo journalctl -u wire-pod -n 30 --no-pager || true
		rollback_wirepod
		exit 1
	fi
}

rollback_wirepod() {
	[ -f /usr/bin/wire-pod.orig ] || { echo "No /usr/bin/wire-pod.orig to restore."; exit 1; }
	sudo systemctl stop wire-pod
	sudo cp -p /usr/bin/wire-pod.orig /usr/bin/wire-pod
	if [ -f /etc/wire-pod/wire-pod/apiConfig.json.before-perbot ]; then
		sudo cp -p /etc/wire-pod/wire-pod/apiConfig.json.before-perbot /etc/wire-pod/wire-pod/apiConfig.json
	fi
	sudo systemctl start wire-pod
	sleep 5
	systemctl is-active wire-pod
}

case "${1:-}" in
	server) server ;;
	build-wirepod) build_wirepod ;;
	install-wirepod) install_wirepod ;;
	rollback-wirepod) rollback_wirepod ;;
	*) sed -n '2,9p' "$0"; exit 1 ;;
esac
