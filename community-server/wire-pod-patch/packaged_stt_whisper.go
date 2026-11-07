//go:build whisper

package main

// Speech to text for the packaged build: whisper.cpp on this machine (deploy.sh build-wirepod-whisper).

import stt "github.com/kercre123/wire-pod/chipper/pkg/wirepod/stt/vtcswhisper"

const sttService = "whisper.cpp"

var (
	sttInit = stt.Init
	sttSTT  = stt.STT
	sttName = stt.Name
)
