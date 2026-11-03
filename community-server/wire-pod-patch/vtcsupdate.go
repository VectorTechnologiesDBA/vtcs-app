package wirepod_ttr

// Voice commands for VictorOS updates from VTCS:
//
//	"check for updates"  -> "No new update is out." or "Version X came out N days ago. Do you want to hear the
//	                        release notes, update, or both?" (then Vector listens for the answer)
//	"check update"       -> the progress of an update that's downloading or installing, or "No update is in progress."
//
// The latest release comes from the community server (/vic/latest.json); the robot's own version and update
// progress come from the robot over its SDK connection.

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/fforchino/vector-go-sdk/pkg/vector"
	"github.com/fforchino/vector-go-sdk/pkg/vectorpb"
	"github.com/kercre123/wire-pod/chipper/pkg/logger"
	"github.com/kercre123/wire-pod/chipper/pkg/vars"
)

// The community server on this machine (its certificate is for server.vectortech.dev, not 127.0.0.1).
const vtcsLatestURL = "https://127.0.0.1:8443/vic/latest.json"

type vtcsRelease struct {
	Version  string `json:"version"`
	Released int64  `json:"released"`
	Notes    string `json:"notes"`
}

type vtcsQuestion struct {
	asked   time.Time
	release vtcsRelease
}

var (
	vtcsMu      sync.Mutex
	vtcsPending = map[string]vtcsQuestion{}
	vtcsVersion = regexp.MustCompile(`\d+\.\d+\.\d+\.\d+[a-z]?`)
)

// VTCSUpdateIntent handles "check for updates" and "check update". True if it handled the request.
func VTCSUpdateIntent(req interface{}, voiceText string, esn string) bool {
	t := " " + strings.Join(strings.Fields(strings.ToLower(strings.Trim(voiceText, " .?!"))), " ") + " "
	status := strings.Contains(t, " check update ") || strings.Contains(t, " update status ") ||
		strings.Contains(t, " update progress ") || strings.Contains(t, " how's the update ") ||
		strings.Contains(t, " how is the update ")
	// Anything else about updates ("check for updates", "any updates", just "updates") checks for a new one.
	check := !status && strings.Contains(t, " update")
	if !status && !check {
		return false
	}
	logger.Println("Bot " + esn + " VTCS update command: " + voiceText)
	IntentPass(req, "intent_system_noaudio", voiceText, map[string]string{}, false)
	go func() {
		robot, err := vtcsRobot(esn)
		if err != nil {
			logger.Println("VTCS update: can't reach " + esn + ": " + err.Error())
			return
		}
		if status {
			vtcsSay(robot, vtcsStatusText(robot))
			return
		}
		text, ask, rel := vtcsCheckText(robot)
		vtcsSay(robot, text)
		if ask {
			vtcsMu.Lock()
			vtcsPending[esn] = vtcsQuestion{asked: time.Now(), release: rel}
			vtcsMu.Unlock()
			// Listen for the answer (the robot plays its listening animation); it arrives as a knowledge graph request.
			DoNewRequest(robot)
		}
	}()
	return true
}

// VTCSUpdateAwaitingAnswer: Vector just asked "release notes, update, or both?" and is listening for the answer.
func VTCSUpdateAwaitingAnswer(esn string) bool {
	vtcsMu.Lock()
	defer vtcsMu.Unlock()
	q, ok := vtcsPending[esn]
	if ok && time.Since(q.asked) > time.Minute {
		delete(vtcsPending, esn)
		return false
	}
	return ok
}

// VTCSUpdateAnswer handles the answer to "release notes, update, or both?" and returns what Vector says back.
func VTCSUpdateAnswer(esn string, answer string) string {
	vtcsMu.Lock()
	q := vtcsPending[esn]
	delete(vtcsPending, esn)
	vtcsMu.Unlock()
	a := strings.ToLower(answer)
	both := strings.Contains(a, "both")
	notes := both || strings.Contains(a, "note") || strings.Contains(a, "hear") || strings.Contains(a, "what's new")
	update := both || strings.Contains(a, "update") || strings.Contains(a, "install") || strings.Contains(a, "yes")
	if !notes && !update {
		return "Okay. Say check for updates whenever you're ready."
	}
	var parts []string
	if notes {
		if n := strings.TrimSpace(q.release.Notes); n != "" {
			parts = append(parts, "Here's what's new. "+n)
		} else {
			parts = append(parts, "This release doesn't have any notes.")
		}
	}
	if update {
		robot, err := vtcsRobot(esn)
		if err == nil {
			_, err = robot.Conn.StartUpdateEngine(context.Background(), &vectorpb.CheckUpdateStatusRequest{})
		}
		if err != nil {
			logger.Println("VTCS update: couldn't start the update on " + esn + ": " + err.Error())
			parts = append(parts, "I couldn't start the update. Try again from the Vector app.")
		} else {
			parts = append(parts, "Okay, I'm downloading the update now. Say check update to hear how it's going.")
		}
	}
	return strings.Join(parts, " ")
}

func vtcsCheckText(robot *vector.Vector) (string, bool, vtcsRelease) {
	rel, err := vtcsLatest()
	if err != nil {
		logger.Println("VTCS update: " + err.Error())
		return "I couldn't check for updates right now. Try again later.", false, rel
	}
	current := ""
	if vs, err := robot.Conn.VersionState(context.Background(), &vectorpb.VersionStateRequest{}); err == nil {
		current = vtcsVersion.FindString(vs.OsVersion)
	}
	if rel.Version == "" || (current != "" && vtcsCompare(rel.Version, current) <= 0) {
		return "No new update is out.", false, rel
	}
	return fmt.Sprintf("Version %s was released %s. Do you want to hear the release notes, update, or both?",
		vtcsSpeakVersion(rel.Version), vtcsAgo(rel.Released)), true, rel
}

func vtcsStatusText(robot *vector.Vector) string {
	st, err := robot.Conn.CheckUpdateStatus(context.Background(), &vectorpb.CheckUpdateStatusRequest{})
	if err != nil {
		return "I couldn't check on the update right now."
	}
	name := st.UpdateStatus.String()
	version := ""
	if st.UpdateVersion != "" {
		version = " version " + vtcsSpeakVersion(st.UpdateVersion)
	}
	percent := ""
	if st.Expected > 0 && st.Progress >= 0 {
		percent = fmt.Sprintf(" It's %d percent done.", st.Progress*100/st.Expected)
	}
	switch {
	case strings.Contains(name, "READY_TO_REBOOT"), strings.Contains(name, "READY_TO_INSTALL"):
		return "The update to" + version + " is ready. I'll finish installing it the next time I restart."
	case strings.Contains(name, "DOWNLOAD"):
		return "I'm downloading" + version + "." + percent
	case strings.Contains(name, "IN_PROGRESS"):
		return "I'm installing" + version + "." + percent
	case strings.Contains(name, "FAILURE"):
		return "The last update didn't finish. Say check for updates to try again."
	}
	return "No update is in progress."
}

func vtcsLatest() (vtcsRelease, error) {
	var rel vtcsRelease
	client := http.Client{Timeout: 10 * time.Second, Transport: &http.Transport{
		TLSClientConfig: &tls.Config{InsecureSkipVerify: true}}}
	resp, err := client.Get(vtcsLatestURL)
	if err != nil {
		return rel, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return rel, nil
	}
	if resp.StatusCode != http.StatusOK {
		return rel, fmt.Errorf("latest.json: HTTP %d", resp.StatusCode)
	}
	return rel, json.NewDecoder(resp.Body).Decode(&rel)
}

// vtcsCompare compares VictorOS versions like 3.0.2.44d by their numbers.
func vtcsCompare(a, b string) int {
	pa, pb := strings.Split(strings.TrimRight(a, "abcdefghijklmnopqrstuvwxyz"), "."),
		strings.Split(strings.TrimRight(b, "abcdefghijklmnopqrstuvwxyz"), ".")
	for i := 0; i < len(pa) || i < len(pb); i++ {
		var x, y int
		if i < len(pa) {
			x, _ = strconv.Atoi(pa[i])
		}
		if i < len(pb) {
			y, _ = strconv.Atoi(pb[i])
		}
		if x != y {
			if x < y {
				return -1
			}
			return 1
		}
	}
	return 0
}

// "3.0.2.44d" -> "3 point 0 point 2 point 44", which the robot's voice reads naturally.
func vtcsSpeakVersion(v string) string {
	return strings.ReplaceAll(strings.TrimRight(v, "abcdefghijklmnopqrstuvwxyz"), ".", " point ")
}

func vtcsAgo(unix int64) string {
	if unix <= 0 {
		return "recently"
	}
	d := time.Since(time.Unix(unix, 0))
	days := int(d.Hours() / 24)
	plural := func(n int, unit string) string {
		if n == 1 {
			return "1 " + unit + " ago"
		}
		return fmt.Sprintf("%d %ss ago", n, unit)
	}
	switch {
	case d < time.Hour:
		return "just now"
	case days < 1:
		return plural(int(d.Hours()), "hour")
	case days == 1:
		return "yesterday"
	case days < 14:
		return plural(days, "day")
	case days < 60:
		return plural(days/7, "week")
	case days < 730:
		return plural(days/30, "month")
	}
	return plural(days/365, "year")
}

func vtcsRobot(esn string) (*vector.Vector, error) {
	for _, bot := range vars.BotInfo.Robots {
		if bot.Esn == esn {
			return vector.New(vector.WithSerialNo(esn), vector.WithToken(bot.GUID),
				vector.WithTarget(vars.RobotTarget(bot.Esn, bot.IPAddress)))
		}
	}
	return nil, fmt.Errorf("robot not known to wire-pod")
}

// vtcsSay takes behavior control, says the text in Vector's voice with his talking animations, and lets go.
func vtcsSay(robot *vector.Vector, text string) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	r, err := robot.Conn.BehaviorControl(ctx)
	if err != nil {
		logger.Println("VTCS update: behavior control: " + err.Error())
		return
	}
	if err := r.Send(&vectorpb.BehaviorControlRequest{RequestType: &vectorpb.BehaviorControlRequest_ControlRequest{
		ControlRequest: &vectorpb.ControlRequest{Priority: vectorpb.ControlRequest_OVERRIDE_BEHAVIORS}}}); err != nil {
		return
	}
	for {
		resp, err := r.Recv()
		if err != nil {
			return
		}
		if resp.GetControlGrantedResponse() != nil {
			break
		}
	}
	robot.Conn.PlayAnimation(ctx, &vectorpb.PlayAnimationRequest{
		Animation: &vectorpb.Animation{Name: "anim_getin_tts_01"}, Loops: 1})
	robot.Conn.SayText(ctx, &vectorpb.SayTextRequest{Text: text, UseVectorVoice: true, DurationScalar: 1.0})
	r.Send(&vectorpb.BehaviorControlRequest{RequestType: &vectorpb.BehaviorControlRequest_ControlRelease{
		ControlRelease: &vectorpb.ControlRelease{}}})
}
