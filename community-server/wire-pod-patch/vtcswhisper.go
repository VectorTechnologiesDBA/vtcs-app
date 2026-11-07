// Package vtcswhisper is wire-pod's speech to text with whisper.cpp, running on the server itself (no cloud).
// Built in with `go build -tags whisper` (deploy.sh build-wirepod-whisper); without the tag wire-pod uses Vosk.
//
// What it does differently from wire-pod's own whisper.cpp engine:
//   - one transcription at a time (whisper.cpp's context isn't safe to share between robots talking at once)
//   - it tells Whisper the robot's VIM contact names first, so "Jacek" comes out as Jacek, not "jae sick"
//   - punctuation is stripped and Whisper's silence hallucinations ("[BLANK_AUDIO]", "thanks for watching")
//     are dropped, so intents see plain words like they did with Vosk
package vtcswhisper

import (
	"encoding/binary"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"os"
	"runtime"
	"strings"
	"sync"
	"time"

	whisper "github.com/kercre123/whisper.cpp/bindings/go"
	"github.com/kercre123/wire-pod/chipper/pkg/logger"
	sr "github.com/kercre123/wire-pod/chipper/pkg/wirepod/speechrequest"
)

var Name string = "whisper.cpp"

const defaultModel = "/etc/wire-pod/whisper/ggml-base.en.bin"

var (
	mu     sync.Mutex // whisper.cpp has one brain and it can only think about one robot at a time
	wctx   *whisper.Context
	params whisper.Params

	namesMu sync.Mutex
	names   = map[string]cachedNames{} // esn -> contact names, for the prompt
	client  = http.Client{Timeout: 2 * time.Second}
)

type cachedNames struct {
	list []string
	at   time.Time
}

func Init() error {
	model := os.Getenv("WHISPER_MODEL_FILE")
	if model == "" {
		model = defaultModel
	}
	if _, err := os.Stat(model); err != nil {
		return fmt.Errorf("whisper model not found: %s (run deploy.sh build-wirepod-whisper)", model)
	}
	logger.Println("Opening Whisper model (" + model + ")")
	wctx = whisper.Whisper_init(model)
	params = wctx.Whisper_full_default_params(whisper.SamplingStrategy(whisper.SAMPLING_GREEDY))
	params.SetTranslate(false)
	params.SetPrintSpecial(false)
	params.SetPrintProgress(false)
	params.SetPrintRealtime(false)
	params.SetPrintTimestamps(false)
	params.SetThreads(runtime.NumCPU())
	params.SetNoContext(true)
	params.SetSingleSegment(true)
	params.SetLanguage(wctx.Whisper_lang_id("en"))
	return nil
}

func STT(req sr.SpeechRequest) (string, error) {
	logger.Println("(Bot " + req.Device + ", Whisper) Processing...")
	for {
		if _, err := req.GetNextStreamChunk(); err != nil {
			return "", err
		}
		if done, _ := req.DetectEndOfSpeech(); done {
			break
		}
	}
	start := time.Now()
	text := transcribe(toFloat32(padPCM(req.DecodedMicData)), promptFor(req.Device))
	logger.Println(fmt.Sprintf("Bot %s Transcribed text: %s (%.1fs)", req.Device, text, time.Since(start).Seconds()))
	return text, nil
}

// transcribe runs Whisper on 16 kHz mono audio, one robot at a time, and cleans up what it heard.
func transcribe(audio []float32, prompt string) string {
	mu.Lock()
	defer mu.Unlock()
	var text string
	params.SetInitialPrompt(prompt)
	wctx.Whisper_full(params, audio, nil, func(_ int) {
		text = strings.TrimSpace(wctx.Whisper_full_get_segment_text(0))
	}, nil)
	return clean(text)
}

// promptFor: a short made-up conversation with the robot's contact names in it. Whisper takes the hint and spells
// names the way they're written. Cached a minute per robot so we don't ask the server on every "Hey Vector".
func promptFor(esn string) string {
	esn = strings.ToLower(esn)
	namesMu.Lock()
	c, ok := names[esn]
	namesMu.Unlock()
	if !ok || time.Since(c.at) > time.Minute {
		c = cachedNames{list: fetchNames(esn), at: time.Now()}
		namesMu.Lock()
		names[esn] = c
		namesMu.Unlock()
	}
	p := "Hey Vector. What's the weather? Set a timer for five minutes."
	if len(c.list) > 0 {
		p += " Text " + strings.Join(c.list, ". Text ") + "."
	}
	return p
}

func fetchNames(esn string) []string {
	resp, err := client.Get("http://127.0.0.1:8089/vim/contacts?esn=" + esn)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	var out struct {
		Contacts []struct {
			Name string `json:"name"`
		} `json:"contacts"`
	}
	if json.NewDecoder(resp.Body).Decode(&out) != nil {
		return nil
	}
	var list []string
	for _, c := range out.Contacts {
		if c.Name != "" && len(list) < 20 {
			list = append(list, c.Name)
		}
	}
	return list
}

// Things Whisper "hears" in silence because it watched too much YouTube during training.
var hallucinations = []string{"thanks for watching", "thank you for watching", "subtitles by", "please subscribe",
	"like and subscribe", "transcribed by", "the end"}

func clean(text string) string {
	text = strings.ToLower(text)
	// [BLANK_AUDIO], (music), *silence*: tags, not words
	var b strings.Builder
	depth := 0
	for _, r := range text {
		switch r {
		case '[', '(', '*':
			if r == '*' && depth > 0 {
				depth--
				continue
			}
			depth++
			continue
		case ']', ')':
			if depth > 0 {
				depth--
			}
			continue
		}
		if depth == 0 {
			b.WriteRune(r)
		}
	}
	text = strings.Map(func(r rune) rune {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9', r == '\'', r == ' ':
			return r
		case r == '-', r == '\n', r == '\t':
			return ' '
		}
		return -1
	}, b.String())
	text = strings.Join(strings.Fields(text), " ")
	for _, h := range hallucinations {
		if strings.HasPrefix(text, h) {
			return ""
		}
	}
	return text
}

// Whisper wants at least a second of audio; short "yes" answers get padded with silence.
func padPCM(data []byte) []byte {
	const minBytes = 16000 * 1020 / 1000 * 2
	if len(data) >= minBytes {
		return data
	}
	return append(data, make([]byte, minBytes-len(data))...)
}

func toFloat32(buf []byte) []float32 {
	out := make([]float32, len(buf)/2)
	factor := math.Pow(2, 15)
	for i := range out {
		out[i] = float32(float64(int16(binary.LittleEndian.Uint16(buf[i*2:]))) / factor)
	}
	return out
}
