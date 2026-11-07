package wirepod_ttr

// VIM, Vector Instant Messaging. Contacts and the message queue are kept by the community server (vpp_server.py,
// class Vim); people add each other's robots in the VTCS app with an add code.
//
//	"text Bob"                 -> "What do you want to say to Bob?", then Vector listens for the message
//	"text Bob I'm on my way"   -> "I heard: I'm on my way. Is that correct?" yes sends, no lets you say it again
//	"learn how to say Jacek"   -> "How do you say Jacek?", then "I heard jae sick. Is that correct?" and from then
//	                              on "jae sick" means Jacek (kept by the server, per contact)
//	(also "message Bob", "send a text/message to Bob", "send Bob a message")
//
// Messages for robots on this wire-pod (said to a robot or typed in the app) are announced on the recipient:
// "Message from Hayes. I'm on my way."

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/kercre123/wire-pod/chipper/pkg/logger"
	"github.com/kercre123/wire-pod/chipper/pkg/vars"
)

// The community server's VIM port (127.0.0.1 only).
const vimURL = "http://127.0.0.1:8089"

type vimContact struct {
	Esn    string   `json:"esn"`
	Name   string   `json:"name"`
	Sounds []string `json:"sounds"` // how speech-to-text hears the name, learned by voice
}

type vimMessage struct {
	ID       string `json:"id"`
	To       string `json:"to"`
	FromName string `json:"from_name"`
	Text     string `json:"text"`
}

// What Vector is waiting to hear back.
const (
	vimAskMessage = iota // "What do you want to say to Bob?"
	vimAskSend           // "I heard: .... Is that correct?"
	vimAskName           // "How do you say Jacek?"
	vimAskNameOK         // "I heard jae sick. Is that correct?"
)

type vimQuestion struct {
	asked time.Time
	stage int
	to    vimContact
	text  string // the message, or how the name was heard
}

var (
	vimMu      sync.Mutex
	vimPending = map[string]vimQuestion{}
	vimClient  = http.Client{Timeout: 10 * time.Second}
	// Phrases that start a message, longest first; the contact's name follows.
	// Longest first, otherwise "send a message to Bob" becomes a message to a contact named "a message to Bob",
	// and nobody wants to explain that to Bob.
	vimTriggers = []string{"send a text message to", "send a message to", "send a text to", "send text to",
		"send message to", "text message", "a text message to", "a message to", "a text to", "message to", "text to",
		"send a", "send", "message", "text", "texts"} // the "a ..." ones: Vosk ate the "send"
	vimYes    = []string{"yes", "yeah", "yep", "yup", "correct", "right", "sure", "okay", "ok", "send it", "that's right", "it is"}
	vimNo     = []string{"no", "nope", "nah", "wrong", "incorrect", "not", "that's wrong"}
	vimCancel = []string{"", "cancel", "never mind", "nevermind", "nothing", "stop", "forget it", "quit"}
)

// vimIs: the words are one of the answers, or start with one ("yes please", "no that's wrong").
// Vector does not do "maybe". Vector is a robot of conviction.
func vimIs(words []string, answers []string) bool {
	t := strings.Join(words, " ")
	for _, a := range answers {
		if t == a || (a != "" && strings.HasPrefix(t, a+" ")) {
			return true
		}
	}
	return false
}

func vimAsk(esn string, q vimQuestion) {
	q.asked = time.Now()
	vimMu.Lock()
	vimPending[esn] = q
	vimMu.Unlock()
}

// VTCSVimIntent handles "text <contact>" and the message after "What do you want to say to <contact>?".
// True if it handled the request.
func VTCSVimIntent(req interface{}, voiceText string, botSerial string) bool {
	esn := strings.ToLower(botSerial)
	words := strings.Fields(strings.ToLower(strings.ReplaceAll(strings.Trim(voiceText, " .?!"), ",", "")))

	vimMu.Lock()
	q, waiting := vimPending[esn]
	delete(vimPending, esn)
	vimMu.Unlock()
	if waiting && time.Since(q.asked) < time.Minute {
		logger.Println("Bot " + esn + " VIM answer (" + q.to.Name + "): " + voiceText)
		robot, err := vtcsRobot(botSerial)
		if err != nil {
			IntentPass(req, "intent_system_noaudio", voiceText, map[string]string{}, false)
			return true
		}
		// The "I panicked and changed my mind" section. Vector respects your right to remain silent.
		if len(words) <= 2 && vimIs(words, vimCancel) {
			vtcsRespond(req, voiceText, robot, "Okay, never mind.", false)
			return true
		}
		said := strings.Trim(voiceText, " .?!")
		switch q.stage {
		case vimAskMessage:
			q.stage, q.text = vimAskSend, said
			vimAsk(esn, q)
			vtcsRespond(req, voiceText, robot, "I heard: "+said+". Is that correct?", true)
		case vimAskSend:
			switch {
			case vimIs(words, vimYes):
				vtcsRespond(req, voiceText, robot, vimSend(esn, q.to, q.text), false)
			case vimIs(words, vimNo):
				q.stage, q.text = vimAskMessage, ""
				vimAsk(esn, q)
				vtcsRespond(req, voiceText, robot, "Okay, say it again.", true)
			default:
				vimAsk(esn, q)
				vtcsRespond(req, voiceText, robot, "Should I send it? Say yes or no.", true)
			}
		case vimAskName:
			q.stage, q.text = vimAskNameOK, strings.Join(words, " ")
			vimAsk(esn, q)
			vtcsRespond(req, voiceText, robot, "I heard "+q.text+". Is that correct?", true)
		case vimAskNameOK:
			switch {
			case vimIs(words, vimYes):
				if vimLearn(esn, q.to, q.text) {
					vtcsRespond(req, voiceText, robot, "Got it. Now I know that's "+q.to.Name+".", false)
				} else {
					vtcsRespond(req, voiceText, robot, "I couldn't save that right now. Try again later.", false)
				}
			case vimIs(words, vimNo):
				q.stage, q.text = vimAskName, ""
				vimAsk(esn, q)
				vtcsRespond(req, voiceText, robot, "Okay, say "+q.to.Name+" again.", true)
			default:
				vimAsk(esn, q)
				vtcsRespond(req, voiceText, robot, "Is that right? Say yes or no.", true)
			}
		}
		return true
	}

	if rest, ok := vimAfterLearn(words); ok {
		return vimLearnStart(req, voiceText, botSerial, esn, rest)
	}

	rest, ok := vimAfterTrigger(words)
	if !ok {
		return false
	}
	contacts, err := vimContacts(esn)
	if err != nil {
		logger.Println("VIM: " + err.Error())
	}
	to, message, found := vimMatch(rest, contacts)
	if !found {
		// "send a ..." / "send ..." without a contact is probably something else
		if words[0] == "send" && !strings.HasPrefix(strings.Join(words, " "), "send a text") &&
			!strings.HasPrefix(strings.Join(words, " "), "send a message") {
			return false
		}
	}
	logger.Println("Bot " + esn + " VIM command: " + voiceText)
	robot, rerr := vtcsRobot(botSerial)
	if rerr != nil {
		IntentPass(req, "intent_system_noaudio", voiceText, map[string]string{}, false)
		return true
	}
	switch {
	case err != nil:
		vtcsRespond(req, voiceText, robot, "I can't reach Vector Tech messaging right now. Try again later.", false)
	case len(contacts) == 0:
		vtcsRespond(req, voiceText, robot, "You don't have anyone to text yet. Add people with their add code in the Vector app.", false)
	case !found:
		name := strings.Join(rest, " ")
		if name == "" {
			vtcsRespond(req, voiceText, robot, "Who do you want to text? Say text, and then their name.", false)
		} else {
			vtcsRespond(req, voiceText, robot, "I don't have anyone called "+name+". You can add people in the Vector app.", false)
		}
	case message != "":
		vimAsk(esn, vimQuestion{stage: vimAskSend, to: to, text: message})
		vtcsRespond(req, voiceText, robot, "I heard: "+message+". Is that correct?", true)
	default:
		vimAsk(esn, vimQuestion{stage: vimAskMessage, to: to})
		vtcsRespond(req, voiceText, robot, "What do you want to say to "+to.Name+"?", true)
	}
	return true
}

// vimAfterLearn: the words after "learn how to say" etc. (the name, as badly heard as ever).
// Vosk hears "say" as all sorts. "learn how to save j sick" was a real request. Nobody was saving anyone.
var vimSayWords = map[string]bool{"say": true, "save": true, "said": true, "says": true, "sae": true, "see": true,
	"sey": true, "stay": true, "set": true, "pronounce": true, "announce": true, "the": true, "name": true}

// Ways "learn" comes out. "going to say jae sick" was also a real request. Vosk was going places.
var vimLearnWords = map[string]bool{"learn": true, "learned": true, "learning": true, "lauren": true, "lauren's": true,
	"learns": true, "earn": true, "burn": true, "turn": true}

func vimAfterLearn(words []string) ([]string, bool) {
	// learn [how] [to|you] <say, or whatever Vosk made of it> [the] [name] <name> [name]
	if len(words) < 3 {
		return nil, false
	}
	var rest []string
	switch {
	case vimLearnWords[words[0]]:
		rest = words[1:]
	case words[0] == "going" && words[1] == "to" && (words[2] == "say" || words[2] == "save" || words[2] == "said"):
		rest = words[2:] // "learn how to say" -> "going to say". Strict, so "going to the store" stays shopping
		if len(rest) < 2 {
			return nil, false
		}
	default:
		return nil, false
	}
	for len(rest) > 0 && (rest[0] == "how" || rest[0] == "to" || rest[0] == "you") {
		rest = rest[1:]
	}
	if len(rest) > 0 && vimSayWords[rest[0]] {
		for len(rest) > 0 && vimSayWords[rest[0]] {
			rest = rest[1:]
		}
	} else if len(rest) > 1 {
		rest = rest[1:] // "sage", "sail", "sway": the say slot, however it was heard
	} else {
		return nil, false // "learn to dance": not ours
	}
	// "learn how to say Jacek's name"
	if n := len(rest); n > 1 && rest[n-1] == "name" {
		rest = rest[:n-1]
	}
	return rest, len(rest) > 0
}

// vimLearnStart: "learn how to say Jacek". The name was probably misheard (that's why we're here), so take the
// closest contact rather than insisting, and say which one, so you can cancel if it picked wrong.
func vimLearnStart(req interface{}, voiceText, botSerial, esn string, rest []string) bool {
	logger.Println("Bot " + esn + " VIM learn: " + voiceText)
	robot, err := vtcsRobot(botSerial)
	if err != nil {
		IntentPass(req, "intent_system_noaudio", voiceText, map[string]string{}, false)
		return true
	}
	contacts, err := vimContacts(esn)
	switch {
	case err != nil:
		vtcsRespond(req, voiceText, robot, "I can't reach Vector Tech messaging right now. Try again later.", false)
	case len(contacts) == 0:
		vtcsRespond(req, voiceText, robot, "You don't have anyone in your contacts yet. Add people in the Vector app.", false)
	default:
		to, _, found := vimMatch(rest, contacts)
		if !found {
			to = vimClosest(strings.Join(rest, ""), contacts)
		}
		vimAsk(esn, vimQuestion{stage: vimAskName, to: to})
		vtcsRespond(req, voiceText, robot, "Okay. How do you say "+to.Name+"?", true)
	}
	return true
}

// vimClosest: the contact whose name is fewest typos from what was heard, spaces ignored.
func vimClosest(heard string, contacts []vimContact) vimContact {
	best, bestD := contacts[0], -1
	for _, c := range contacts {
		d := vimDistance(heard, strings.ReplaceAll(strings.ToLower(c.Name), " ", ""))
		if bestD < 0 || d < bestD {
			best, bestD = c, d
		}
	}
	return best
}

// vimLearn saves how a contact's name was heard, on the community server.
func vimLearn(esn string, to vimContact, heard string) bool {
	b, _ := json.Marshal(map[string]string{"esn": esn, "contact": to.Esn, "heard": heard})
	resp, err := vimClient.Post(vimURL+"/vim/sound", "application/json", bytes.NewReader(b))
	if err != nil {
		return false
	}
	resp.Body.Close()
	return resp.StatusCode == 200
}

// vimAfterTrigger: the words after "text" / "send a message to" etc., if the request starts with one.
func vimAfterTrigger(words []string) ([]string, bool) {
	t := strings.Join(words, " ") + " "
	for _, trig := range vimTriggers {
		if strings.HasPrefix(t, trig+" ") {
			rest := strings.Fields(t[len(trig):])
			if len(rest) > 0 && rest[0] == "to" {
				rest = rest[1:]
			}
			return rest, true
		}
	}
	return nil, false
}

// vimMatch finds the contact whose name the words start with (allowing for small mishearings), and what's said
// after it. "send Bob a message" -> Bob, "".
func vimMatch(words []string, contacts []vimContact) (vimContact, string, bool) {
	var best vimContact
	bestLen := 0
	for _, c := range contacts {
		// the real name, then every way it's been heard ("learn how to say ...")
		for _, spelled := range append([]string{c.Name}, c.Sounds...) {
			name := strings.Fields(strings.ToLower(spelled))
			if len(name) == 0 || len(name) > len(words) || len(name) <= bestLen {
				continue
			}
			match := true
			for i, w := range name {
				if !vimSimilar(strings.TrimSuffix(strings.Trim(words[i], ","), "'s"), w) {
					match = false
					break
				}
			}
			if match {
				best, bestLen = c, len(name)
			}
		}
	}
	if bestLen == 0 {
		// Plan B: sounds like. Vosk doesn't know "Jacek", so it hears "jae sick". Glue the first few heard words
		// together and compare consonant sounds: "jae sick" -> jsk, "Jacek" -> jsk. Close enough for government work.
		for _, c := range contacts {
			// the name and every learned way of hearing it, by sound: learned "j sick" also catches "jay sick"
			for _, spelled := range append([]string{c.Name}, c.Sounds...) {
				want := vimSound(spelled)
				if len(want) < 2 {
					continue
				}
				for n := 1; n <= 3 && n <= len(words); n++ {
					if vimSound(strings.Join(words[:n], "")) == want && n > bestLen {
						best, bestLen = c, n
					}
				}
			}
		}
	}
	if bestLen == 0 {
		return best, "", false
	}
	rest := words[bestLen:]
	r := strings.Join(rest, " ")
	switch r {
	// "send Bob a message" is not the message. If it were, Bob would get "a message" and be very confused.
	case "a message", "a text", "a text message", "message", "text":
		return best, "", true
	}
	for _, lead := range []string{"saying ", "that ", "and say ", "and tell them "} {
		r = strings.TrimPrefix(r, lead)
	}
	return best, r, true
}

// Vosk has never heard a name it couldn't mangle. One wrong letter per six is forgiven; "Alec" is Alex,
// "Bobb" is Bob, and "Grandma Sue" is, against all odds, still Grandma Sue.
func vimSimilar(heard, name string) bool {
	if heard == name {
		return true
	}
	if len(name) < 4 {
		return false
	}
	return vimDistance(heard, name) <= 1+len(name)/6
}

// vimSound boils a name down to its consonant sounds: no vowels, soft c is s, ck/q is k, z is s, ph is f,
// doubles squashed. "Jacek" -> "jsk", "jae sick" -> "jsk", "Katie" -> "kt". A poor man's Soundex, very poor.
func vimSound(s string) string {
	s = strings.ToLower(s)
	var letters []byte
	for i := 0; i < len(s); i++ {
		if s[i] >= 'a' && s[i] <= 'z' {
			letters = append(letters, s[i])
		}
	}
	var out []byte
	for i, ch := range letters {
		next := byte(0)
		if i+1 < len(letters) {
			next = letters[i+1]
		}
		switch ch {
		case 'a', 'e', 'i', 'o', 'u', 'y', 'h', 'w':
			continue
		case 'c':
			if next == 'e' || next == 'i' || next == 'y' {
				ch = 's'
			} else {
				ch = 'k'
			}
		case 'q':
			ch = 'k'
		case 'z':
			ch = 's'
		case 'p':
			if next == 'h' {
				ch = 'f'
			}
		}
		if len(out) == 0 || out[len(out)-1] != ch {
			out = append(out, ch)
		}
	}
	return string(out)
}

// Levenshtein distance: how many typos apart two words are. Written by hand because importing a library to
// count letters felt like hiring a moving company to carry a sandwich.
func vimDistance(a, b string) int {
	prev := make([]int, len(b)+1)
	for j := range prev {
		prev[j] = j
	}
	for i := 1; i <= len(a); i++ {
		cur := make([]int, len(b)+1)
		cur[0] = i
		for j := 1; j <= len(b); j++ {
			cost := 1
			if a[i-1] == b[j-1] {
				cost = 0
			}
			cur[j] = prev[j-1] + cost
			if v := prev[j] + 1; v < cur[j] {
				cur[j] = v
			}
			if v := cur[j-1] + 1; v < cur[j] {
				cur[j] = v
			}
		}
		prev = cur
	}
	return prev[len(b)]
}

func vimContacts(esn string) ([]vimContact, error) {
	resp, err := vimClient.Get(vimURL + "/vim/contacts?esn=" + esn)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var out struct {
		Contacts []vimContact `json:"contacts"`
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("contacts: HTTP %d", resp.StatusCode)
	}
	err = json.NewDecoder(resp.Body).Decode(&out)
	return out.Contacts, err
}

// vimSend queues the message and returns what Vector says about it.
func vimSend(esn string, to vimContact, text string) string {
	body, _ := json.Marshal(map[string]string{"from": esn, "to": to.Esn, "text": strings.TrimSpace(text)})
	resp, err := vimClient.Post(vimURL+"/vim/send", "application/json", bytes.NewReader(body))
	if err != nil {
		logger.Println("VIM send: " + err.Error())
		return "I couldn't send that right now. Try again later."
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		var e struct {
			Message string `json:"message"`
		}
		json.NewDecoder(resp.Body).Decode(&e)
		return "I couldn't send that. " + e.Message
	}
	return "Sent to " + to.Name + "."
}

func init() {
	go vimAnnouncer()
}

// vimAnnouncer says waiting messages on the robots this wire-pod knows. A robot that can't be reached is tried
// again a minute later; its messages stay queued on the server until then.
// The tiny mail carrier who lives inside wire-pod. Neither rain, nor snow, nor a robot asleep on its charger
// stays this goroutine from the swift completion of its appointed rounds (every 5 seconds).
func vimAnnouncer() {
	retry := map[string]time.Time{}
	for {
		time.Sleep(5 * time.Second)
		resp, err := vimClient.Get(vimURL + "/vim/pending")
		if err != nil {
			continue
		}
		var out struct {
			Messages []vimMessage `json:"messages"`
		}
		err = json.NewDecoder(resp.Body).Decode(&out)
		resp.Body.Close()
		if err != nil {
			continue
		}
		for _, m := range out.Messages {
			if time.Now().Before(retry[m.To]) || !vimKnown(m.To) {
				continue
			}
			robot, err := vtcsRobot(vimEsn(m.To))
			if err == nil && vtcsSay(robot, "Message from "+m.FromName+". "+m.Text, nil) {
				logger.Println("VIM: announced a message on " + m.To)
				body, _ := json.Marshal(map[string]string{"id": m.ID})
				if r, err := vimClient.Post(vimURL+"/vim/delivered", "application/json", bytes.NewReader(body)); err == nil {
					r.Body.Close()
				}
				continue
			}
			// Nobody home. Leave it on the porch and knock again in a minute, like a polite but persistent delivery guy.
			retry[m.To] = time.Now().Add(time.Minute)
		}
	}
}

func vimKnown(esn string) bool {
	for _, bot := range vars.BotInfo.Robots {
		if strings.EqualFold(bot.Esn, esn) {
			return true
		}
	}
	return false
}

// vimEsn: the ESN as wire-pod spells it.
func vimEsn(esn string) string {
	for _, bot := range vars.BotInfo.Robots {
		if strings.EqualFold(bot.Esn, esn) {
			return bot.Esn
		}
	}
	return esn
}
