package vars

import (
	"net"
	"os"
	"path/filepath"
	"strings"
)

// RobotTarget returns the address to reach a robot's control API (vic-gateway) at. Robots on the server's own
// network are reached directly; robots elsewhere through the VTCS relay, once the community server has verified the
// robot behind the link and written its local address to $VTCS_RELAY_OK_DIR/<esn> (default /run/vtcs-relay-ok).
func RobotTarget(esn string, ip string) string {
	direct := ip + ":443"
	if parsed := net.ParseIP(ip); parsed != nil && (parsed.IsPrivate() || parsed.IsLoopback()) {
		return direct
	}
	esn = strings.ToLower(strings.TrimSpace(esn))
	if !perBotESN.MatchString(esn) {
		return direct
	}
	dir := os.Getenv("VTCS_RELAY_OK_DIR")
	if dir == "" {
		dir = "/run/vtcs-relay-ok"
	}
	if data, err := os.ReadFile(filepath.Join(dir, esn)); err == nil {
		if addr := strings.TrimSpace(string(data)); addr != "" {
			return addr
		}
	}
	return direct
}
