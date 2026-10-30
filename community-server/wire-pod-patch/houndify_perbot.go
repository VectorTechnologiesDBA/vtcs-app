package processreqs

import (
	"sync"

	"github.com/kercre123/wire-pod/chipper/pkg/logger"
	"github.com/kercre123/wire-pod/chipper/pkg/vars"
	"github.com/soundhound/houndify-sdk-go"
)

// One Houndify client per client ID and key, so each robot can use its owner's Houndify account (see
// vars.KnowledgeFor). Robots on the same account share a client.

var houndifyClients = map[string]*houndify.Client{}
var houndifyClientsMu sync.Mutex

// houndifyFor returns the Houndify client for a robot, or nil if Houndify isn't its knowledge graph provider.
func houndifyFor(esn string) *houndify.Client {
	kc := vars.KnowledgeFor(esn)
	if !kc.Enable || kc.Provider != "houndify" {
		return nil
	}
	if kc.ID == "" || kc.Key == "" {
		logger.Println("Houndify Client Key or ID is empty for " + esn)
		return nil
	}
	houndifyClientsMu.Lock()
	defer houndifyClientsMu.Unlock()
	k := kc.ID + "\x00" + kc.Key
	c, ok := houndifyClients[k]
	if !ok {
		c = &houndify.Client{ClientID: kc.ID, ClientKey: kc.Key}
		c.EnableConversationState()
		houndifyClients[k] = c
		logger.Println("Initialized Houndify client for " + esn)
	}
	return c
}
