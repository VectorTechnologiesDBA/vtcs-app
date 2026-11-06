package wirepod_ttr

// VIM, Vector Instant Messaging. Contacts and the message queue are kept by the community server (vpp_server.py,
// class Vim); people add each other's robots in the VTCS app with an add code.
//
//	"text Bob"                 -> "What do you want to say to Bob?", then Vector listens for the message
//	"text Bob I'm on my way"   -> sent straight away
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
	Esn  string `json:"esn"`
	Name string `json:"name"`
}

type vimMessage struct {
	ID       string `json:"id"`
	To       string `json:"to"`
	FromName string `json:"from_name"`
	Text     string `json:"text"`
}

type vimQuestion struct {
	asked time.Time
	to    vimContact
}

var (
	vimMu      sync.Mutex
	vimPending = map[string]vimQuestion{}
	vimClient  = http.Client{Timeout: 10 * time.Second}
	// Phrases that start a message, longest first; the contact's name follows.
	// Longest first, otherwise "send a message to Bob" becomes a message to a contact named "a message to Bob",
	// and nobody wants to explain that to Bob.
	vimTriggers = []string{"send a text message to", "send a message to", "send a text to", "send text to",
		"send message to", "text message", "send a", "send", "message", "text", "texts"}
)

// VTCSVimIntent handles "text <contact>" and the message after "What do you want to say to <contact>?".
// True if it handled the request.
func VTCSVimIntent(req interface{}, voiceText string, botSerial string) bool {
	esn := strings.ToLower(botSerial)
	words := strings.Fields(strings.ToLower(strings.Trim(voiceText, " .?!")))

	vimMu.Lock()
	q, waiting := vimPending[esn]
	delete(vimPending, esn)
	vimMu.Unlock()
	if waiting && time.Since(q.asked) < time.Minute {
		logger.Println("Bot " + esn + " VIM message for " + q.to.Name + ": " + voiceText)
		robot, err := vtcsRobot(botSerial)
		if err != nil {
			IntentPass(req, "intent_system_noaudio", voiceText, map[string]string{}, false)
			return true
		}
		text := strings.Join(words, " ")
		switch text {
		// The "I panicked and changed my mind" section. Vector respects your right to remain silent.
		case "", "cancel", "never mind", "nevermind", "nothing", "stop":
			vtcsRespond(req, voiceText, robot, "Okay, I won't send anything.", false)
		default:
			vtcsRespond(req, voiceText, robot, vimSend(esn, q.to, voiceText), false)
		}
		return true
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
		vtcsRespond(req, voiceText, robot, vimSend(esn, to, message), false)
	default:
		vimMu.Lock()
		vimPending[esn] = vimQuestion{asked: time.Now(), to: to}
		vimMu.Unlock()
		vtcsRespond(req, voiceText, robot, "What do you want to say to "+to.Name+"?", true)
	}
	return true
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
		name := strings.Fields(strings.ToLower(c.Name))
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
