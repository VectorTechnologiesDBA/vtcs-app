//go:build !whisper

package main

// Speech to text for the packaged build: Vosk (the default). Build with -tags whisper for whisper.cpp instead.

import stt "github.com/kercre123/wire-pod/chipper/pkg/wirepod/stt/vosk"

const sttService = "vosk"

var (
	sttInit = stt.Init
	sttSTT  = stt.STT
	sttName = stt.Name
)
