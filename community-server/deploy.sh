#!/bin/bash
# Deploy the community server and the per-robot wire-pod patch on the server (Raspberry Pi), from a copy of this
# folder (e.g. ~/vpp-deploy).
#
#   bash deploy.sh server            install vpp_server.py, create wire-pod's per-bot folder, restart vpp-server
#   bash deploy.sh build-wirepod     build a patched wire-pod in ~/wire-pod-build (the running one isn't touched)
#   bash deploy.sh build-wirepod-whisper   the same, with whisper.cpp speech to text instead of Vosk (much better
#                                    at names; WHISPER_MODEL=base.en by default, tiny.en if base is too slow)
#   bash deploy.sh vosk-model big    swap Vosk's model: big (1.8 GB, most accurate), lgraph (128 MB), small (stock)
#   bash deploy.sh install-wirepod   swap it in for /usr/bin/wire-pod; puts the old one back if it doesn't start
#   bash deploy.sh rollback-wirepod  put the original /usr/bin/wire-pod back
#   bash deploy.sh relay             set up the VTCS relay (sshd on port 2222) for robots on other networks
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ME=$(id -un)
PERBOT=/etc/wire-pod/per-bot
BUILD=$HOME/wire-pod-build
VOSK_VER=0.3.45
STT=vosk
WHISPER_SRC=$HOME/whisper.cpp-src
WHISPER_COMMIT=60cd96acff3a   # the commit wire-pod's Go binding (go.mod) is pinned to; they must match
WHISPER_MODEL=${WHISPER_MODEL:-base.en}
WHISPER_DIR=/etc/wire-pod/whisper

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

relay() {
	say "VTCS relay: robots on other networks keep a link to this server (port 2222) so the extras reach them"
	[ -f "$HERE/relay/ssh_host_ed25519_key" ] || { echo "Missing $HERE/relay/ssh_host_ed25519_key (copy the relay folder over)."; exit 1; }
	id vtcs-relay >/dev/null 2>&1 || sudo useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin vtcs-relay
	# No password, but not "locked" either: sshd refuses locked accounts outright. Keys are the only way in.
	sudo usermod -p '*' vtcs-relay
	sudo install -d -m 755 /etc/vtcs-relay
	sudo install -m 600 "$HERE/relay/ssh_host_ed25519_key" /etc/vtcs-relay/ssh_host_ed25519_key
	sudo install -m 644 -t /etc/vtcs-relay "$HERE/relay/ssh_host_ed25519_key.pub" "$HERE/relay/authorized_keys" "$HERE/relay/sshd_config"
	# Robot links land in /run/vtcs-relay (written by sshd as vtcs-relay); vpp-server marks verified ones in /run/vtcs-relay-ok.
	printf 'd /run/vtcs-relay 0755 vtcs-relay vtcs-relay -\nd /run/vtcs-relay-ok 0755 %s %s -\n' "$ME" "$ME" | sudo tee /etc/tmpfiles.d/vtcs-relay.conf >/dev/null
	sudo systemd-tmpfiles --create /etc/tmpfiles.d/vtcs-relay.conf
	sudo install -m 644 "$HERE/relay/vtcs-relay-sshd.service" /etc/systemd/system/vtcs-relay-sshd.service
	sudo systemctl daemon-reload
	sudo systemctl enable vtcs-relay-sshd >/dev/null
	sudo systemctl restart vtcs-relay-sshd
	sleep 2
	systemctl is-active vtcs-relay-sshd
	echo "Done. Forward TCP port 2222 on the router to this machine."
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
	if [ "$STT" = whisper ]; then
		whisper_cpp
	fi
	say "wire-pod source"
	rm -rf "$BUILD"
	git clone --depth 1 --progress https://github.com/kercre123/wire-pod.git "$BUILD"
	echo "installed wire-pod: $(cat /etc/wire-pod/version 2>/dev/null)   source: $(git -C "$BUILD" log -1 --format='%h %ad' --date=short)"
	say "Applying the per-robot patch"
	python3 "$HERE/wire-pod-patch/apply.py" "$BUILD"
	say "Building (takes a while on a Pi)"
	cd "$BUILD/chipper"
	local tags=nolibopusfile
	if [ "$STT" = whisper ]; then
		tags="nolibopusfile whisper"
		export CGO_ENABLED=1 CGO_CFLAGS="-I$WHISPER_SRC/include -I$WHISPER_SRC/ggml/include"
		CGO_LDFLAGS=$(find "$WHISPER_SRC/build" -name "*.a" -printf '-L%h\n' | sort -u | tr '\n' ' ')
		export CGO_LDFLAGS
	else
		export CGO_ENABLED=1 CGO_CFLAGS="-I$HOME/.vosk/libvosk" CGO_LDFLAGS="-L$HOME/.vosk/libvosk -lvosk -ldl -lpthread"
	fi
	go build -tags "$tags" \
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

# whisper.cpp, built once into static libraries (~10 minutes on a Pi 5), plus the model wire-pod loads.
whisper_cpp() {
	sudo apt-get install -y -qq cmake >/dev/null
	if [ ! -f "$WHISPER_SRC/build/.vtcs-ok" ] || [ "$(git -C "$WHISPER_SRC" rev-parse --short=12 HEAD 2>/dev/null)" != "$WHISPER_COMMIT" ]; then
		say "whisper.cpp (speech to text, built on this machine; this is the slow part)"
		rm -rf "$WHISPER_SRC"
		git clone -q https://github.com/kercre123/whisper.cpp.git "$WHISPER_SRC"
		git -C "$WHISPER_SRC" checkout -q "$WHISPER_COMMIT"
		cmake -S "$WHISPER_SRC" -B "$WHISPER_SRC/build" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
			-DCMAKE_POSITION_INDEPENDENT_CODE=ON -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_EXAMPLES=OFF >/dev/null
		cmake --build "$WHISPER_SRC/build" -j"$(nproc)" --config Release
		touch "$WHISPER_SRC/build/.vtcs-ok"
	fi
	if [ ! -f "$WHISPER_DIR/ggml-$WHISPER_MODEL.bin" ]; then
		say "Whisper model $WHISPER_MODEL"
		bash "$WHISPER_SRC/models/download-ggml-model.sh" "$WHISPER_MODEL" >/dev/null
		sudo install -d -m 755 "$WHISPER_DIR"
		sudo install -m 644 "$WHISPER_SRC/models/ggml-$WHISPER_MODEL.bin" "$WHISPER_DIR/ggml-$WHISPER_MODEL.bin"
	fi
	# the engine reads ggml-base.en.bin unless told otherwise; point that name at the chosen model
	if [ "$WHISPER_MODEL" != base.en ]; then
		sudo ln -sf "$WHISPER_DIR/ggml-$WHISPER_MODEL.bin" "$WHISPER_DIR/ggml-base.en.bin"
	fi
	ls -la "$WHISPER_DIR"
}

# Vosk's speech model. wire-pod ships the small one, which is quick but mishears a lot; a Pi 5 can run the big one.
vosk_model() {
	local which=${1:-big} name dir=/etc/wire-pod/wire-pod/vosk/models/en-US
	case "$which" in
		big) name=vosk-model-en-us-0.22 ;;
		lgraph) name=vosk-model-en-us-0.22-lgraph ;;
		small) name=vosk-model-small-en-us-0.15 ;;
		*) echo "vosk-model big | lgraph | small"; exit 1 ;;
	esac
	if [ ! -d "$HOME/vosk-models/$name" ]; then
		say "Downloading $name (the big one is 1.8 GB; this takes a while)"
		mkdir -p "$HOME/vosk-models"
		wget -q --show-progress -O "$HOME/vosk-models/$name.zip" "https://alphacephei.com/vosk/models/$name.zip"
		unzip -q "$HOME/vosk-models/$name.zip" -d "$HOME/vosk-models"
		rm "$HOME/vosk-models/$name.zip"
	fi
	say "Installing $name as wire-pod's English model"
	sudo systemctl stop wire-pod
	if [ -d "$dir/model" ] && [ ! -e "$dir/model.stock" ] && [ ! -L "$dir/model" ]; then
		sudo mv "$dir/model" "$dir/model.stock"   # the model wire-pod came with, kept
	else
		sudo rm -rf "$dir/model"
	fi
	if [ "$which" = small ] && [ -d "$dir/model.stock" ]; then
		sudo ln -s "$dir/model.stock" "$dir/model"
	else
		sudo ln -s "$HOME/vosk-models/$name" "$dir/model"
	fi
	# the big model has no grammar support; wire-pod would crash at start with it on
	if [ "$which" = big ] && grep -q '^vosk_with_grammer=true' /etc/wire-pod/config.ini 2>/dev/null; then
		sudo sed -i 's/^vosk_with_grammer=true/vosk_with_grammer=false/' /etc/wire-pod/config.ini
		echo "Turned vosk_with_grammer off (the big model doesn't support it)."
	fi
	sudo systemctl start wire-pod
	say "Waiting for wire-pod to load the model"
	for _ in $(seq 1 60); do
		sleep 5
		if echo | openssl s_client -connect 127.0.0.1:443 2>/dev/null | grep -q "BEGIN CERTIFICATE"; then
			echo "wire-pod is up with $name."
			free -h | head -2
			return
		fi
	done
	echo "wire-pod didn't come up in 5 minutes. Logs: journalctl -u wire-pod -n 40   Undo: bash $0 vosk-model small"
	exit 1
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
	for _ in $(seq 1 60); do
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
	build-wirepod-whisper) STT=whisper build_wirepod ;;
	install-wirepod) install_wirepod ;;
	rollback-wirepod) rollback_wirepod ;;
	relay) relay ;;
	vosk-model) vosk_model "${2:-big}" ;;
	*) sed -n '2,10p' "$0"; exit 1 ;;
esac
