package wirepod_ttr

// Quiet hours, set in the VTCS app and kept by the community server (vpp_server.py, class Quiet). VictorOS applies
// them in its sleep cycle; this sends each robot its settings (the "vtcs_quiet" app intent) whenever they change,
// and again after wire-pod restarts, in case the robot missed them.

import (
	"context"
	"encoding/json"
	"strings"
	"time"

	"github.com/fforchino/vector-go-sdk/pkg/vectorpb"
	"github.com/kercre123/wire-pod/chipper/pkg/logger"
)

func init() {
	go quietSender()
}

func quietSender() {
	sent := map[string]int{}        // esn -> rev the robot has
	retry := map[string]time.Time{}  // esn -> don't try before
	for {
		time.Sleep(10 * time.Second)
		resp, err := vimClient.Get(vimURL + "/quiet/all")
		if err != nil {
			continue
		}
		var out struct {
			Robots map[string]struct {
				Rev      int             `json:"rev"`
				Settings json.RawMessage `json:"settings"`
			} `json:"robots"`
		}
		err = json.NewDecoder(resp.Body).Decode(&out)
		resp.Body.Close()
		if err != nil {
			continue
		}
		for esn, q := range out.Robots {
			esn = strings.ToLower(esn)
			if sent[esn] == q.Rev || time.Now().Before(retry[esn]) || !vimKnown(esn) {
				continue
			}
			robot, err := vtcsRobot(vimEsn(esn))
			if err == nil {
				ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
				_, err = robot.Conn.AppIntent(ctx, &vectorpb.AppIntentRequest{Intent: "vtcs_quiet", Param: string(q.Settings)})
				cancel()
			}
			if err != nil {
				// asleep robots still answer; this is an unreachable one. Try again in a while.
				retry[esn] = time.Now().Add(2 * time.Minute)
				continue
			}
			logger.Println("Quiet hours: sent to " + esn)
			sent[esn] = q.Rev
		}
	}
}
