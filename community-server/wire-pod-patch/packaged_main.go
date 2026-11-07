package main

// Entry point for the Linux package layout (/usr/bin/wire-pod, run from /etc/wire-pod): what the .deb's own build
// does, which isn't in wire-pod's public source. Data lives in /etc/wire-pod/wire-pod (apiConfig.json, certs, jdocs,
// session-certs, vosk models); settings come from /etc/wire-pod/config.ini.

import (
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"

	"github.com/kercre123/wire-pod/chipper/pkg/initwirepod"
	"github.com/kercre123/wire-pod/chipper/pkg/logger"
	"github.com/kercre123/wire-pod/chipper/pkg/vars"
)

const configIni = "/etc/wire-pod/config.ini"

func readConfigIni() map[string]string {
	conf := map[string]string{}
	f, err := os.Open(configIni)
	if err != nil {
		logger.Println("Can't find " + configIni + ", assuming port 8080")
		return conf
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	for s.Scan() {
		line := strings.TrimSpace(s.Text())
		if line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, ";") {
			continue
		}
		if k, v, ok := strings.Cut(line, "="); ok {
			conf[strings.TrimSpace(k)] = strings.Trim(strings.TrimSpace(v), `"`)
		}
	}
	return conf
}

// savedSTTLanguage: the language in wire-pod's apiConfig.json, or en-US.
func savedSTTLanguage() string {
	data, err := os.ReadFile("/etc/wire-pod/wire-pod/apiConfig.json")
	if err == nil {
		var c struct {
			STT struct {
				Language string `json:"language"`
			} `json:"STT"`
		}
		if json.Unmarshal(data, &c) == nil && c.STT.Language != "" {
			return c.STT.Language
		}
	}
	return "en-US"
}

func isPi() bool {
	model, err := os.ReadFile("/proc/device-tree/model")
	return err == nil && strings.Contains(string(model), "Raspberry Pi")
}

func setPerformanceGovernor() {
	govs, _ := filepath.Glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")
	for _, g := range govs {
		os.WriteFile(g, []byte("performance"), 0644)
	}
	logger.Println("CPU governor set to performance")
}

func main() {
	vars.Packaged = true
	vars.IsPackagedLinux = true
	// vars.Init puts the data folder in os.UserConfigDir()/wire-pod, i.e. /etc/wire-pod/wire-pod.
	os.Setenv("XDG_CONFIG_HOME", "/etc/wire-pod")
	// vars.ReadConfig rewrites the STT settings (and blanks the language) whenever the saved STT service differs
	// from STT_SERVICE, so it must name this build's engine.
	os.Setenv("STT_SERVICE", sttService)
	// ...and when it does, it takes the language from STT_LANGUAGE. Blank means "not set up", which leaves the voice
	// server off. So switching Vosk <-> Whisper keeps the language the config already had.
	if os.Getenv("STT_LANGUAGE") == "" {
		os.Setenv("STT_LANGUAGE", savedSTTLanguage())
	}

	conf := readConfigIni()
	if p := conf["web_port"]; p != "" {
		os.Setenv("WEBSERVER_PORT", p)
	}
	if conf["vosk_with_grammer"] == "true" {
		os.Setenv("VOSK_WITH_GRAMMER", "true")
	}
	if v := conf["whisper_audio_ctx"]; v != "" { // Whisper speed vs accuracy, see vtcswhisper.audioCtx
		os.Setenv("WHISPER_AUDIO_CTX", v)
	}
	if conf["use_mdns"] == "false" {
		os.Setenv("DISABLE_MDNS", "true")
	}
	switch conf["perf_mode"] {
	case "true":
		setPerformanceGovernor()
	case "onlyifpi", "":
		if isPi() {
			setPerformanceGovernor()
		}
	}

	initwirepod.StartFromProgramInit(sttInit, sttSTT, sttName)
}
