package vars

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
)

// Per-robot weather and knowledge settings, for servers shared by several people (the Vector++ community server
// writes one file per robot, from its owner's account settings). A file holds {"weather": {...}, "knowledge": {...}}
// with the same fields as apiConfig.json; fields it leaves out keep the global value.
//
// Set PERBOT_DIR to change where they're read from (default ./per-bot, i.e. /etc/wire-pod/per-bot when packaged).

var perBotESN = regexp.MustCompile(`^[0-9a-f]{8}$`)

func perBotFile(esn string) ([]byte, bool) {
	if !perBotESN.MatchString(esn) {
		return nil, false
	}
	dir := os.Getenv("PERBOT_DIR")
	if dir == "" {
		dir = "./per-bot"
	}
	data, err := os.ReadFile(filepath.Join(dir, esn+".json"))
	return data, err == nil
}

// WeatherFor returns the weather settings to use for a robot: its owner's, over the global ones.
func WeatherFor(esn string) WeatherConfig {
	cfg := APIConfig.Weather
	if data, ok := perBotFile(esn); ok {
		var f struct {
			Weather *json.RawMessage `json:"weather"`
		}
		if json.Unmarshal(data, &f) == nil && f.Weather != nil {
			json.Unmarshal(*f.Weather, &cfg)
		}
	}
	return cfg
}

// KnowledgeFor returns the knowledge graph settings to use for a robot: its owner's, over the global ones.
func KnowledgeFor(esn string) KnowledgeConfig {
	cfg := APIConfig.Knowledge
	if data, ok := perBotFile(esn); ok {
		var f struct {
			Knowledge *json.RawMessage `json:"knowledge"`
		}
		if json.Unmarshal(data, &f) == nil && f.Knowledge != nil {
			own := cfg
			if json.Unmarshal(*f.Knowledge, &own) == nil {
				cfg = own
			}
		}
	}
	return cfg
}
