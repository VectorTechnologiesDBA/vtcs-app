#!/usr/bin/env python3
"""Apply the per-robot settings patch to a wire-pod checkout: python3 apply.py <wire-pod dir>

Makes wire-pod use each robot's own weather and knowledge graph settings (vars.WeatherFor / vars.KnowledgeFor,
see perbot.go) instead of the global ones, where a robot has them."""

import os
import shutil
import sys

ROOT = os.path.join(sys.argv[1], "chipper", "pkg")
HERE = os.path.dirname(os.path.abspath(__file__))


def edit(rel, pairs):
    path = os.path.join(ROOT, rel)
    with open(path, encoding="utf-8") as f:
        src = f.read()
    for old, new, count in pairs:
        n = src.count(old)
        if n != count:
            sys.exit("%s: expected %d of %r, found %d (wire-pod version differs?)" % (rel, count, old, n))
        src = src.replace(old, new)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print("patched", rel)


def in_func(rel, func_sig, old, new, count):
    """Replace old with new only inside the function starting at func_sig (up to the next top-level func)."""
    path = os.path.join(ROOT, rel)
    with open(path, encoding="utf-8") as f:
        src = f.read()
    start = src.find(func_sig)
    if start < 0:
        sys.exit("%s: no %r" % (rel, func_sig))
    end = src.find("\nfunc ", start + 1)
    end = len(src) if end < 0 else end
    body = src[start:end]
    if body.count(old) != count:
        sys.exit("%s %s: expected %d of %r, found %d" % (rel, func_sig, count, old, body.count(old)))
    src = src[:start] + body.replace(old, new) + src[end:]
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print("patched", rel, func_sig.split("(")[0])


shutil.copy(os.path.join(HERE, "perbot.go"), os.path.join(ROOT, "vars", "perbot.go"))
shutil.copy(os.path.join(HERE, "realpeer.go"), os.path.join(ROOT, "vars", "realpeer.go"))
shutil.copy(os.path.join(HERE, "houndify_perbot.go"), os.path.join(ROOT, "wirepod", "preqs", "houndify_perbot.go"))
# The Linux package's entry point (data in /etc/wire-pod/wire-pod, settings from /etc/wire-pod/config.ini).
os.makedirs(os.path.join(sys.argv[1], "chipper", "cmd", "packaged"), exist_ok=True)
shutil.copy(os.path.join(HERE, "packaged_main.go"), os.path.join(sys.argv[1], "chipper", "cmd", "packaged", "main.go"))

# Named types, so a robot's settings can be returned as a copy.
edit("vars/config.go", [
    ("type apiConfig struct {\n\tWeather struct {", "type WeatherConfig struct {", 1),
    ("\t} `json:\"weather\"`\n\tKnowledge struct {", "}\n\ntype KnowledgeConfig struct {", 1),
    ("\t} `json:\"knowledge\"`\n", "}\n\ntype apiConfig struct {\n\tWeather   WeatherConfig   `json:\"weather\"`\n\tKnowledge KnowledgeConfig `json:\"knowledge\"`\n", 1),
])

# Weather: thread the robot's serial down to getWeather.
edit("wirepod/ttr/weather.go", [
    ("func getWeather(location string,", "func getWeather(esn string, location string,", 1),
    ("\tweatherAPIEnabled := vars.APIConfig.Weather.Enable\n\tweatherAPIKey := vars.APIConfig.Weather.Key\n"
     "\tweatherAPIUnit := vars.APIConfig.Weather.Unit\n\tweatherAPIProvider := vars.APIConfig.Weather.Provider\n",
     "\twc := vars.WeatherFor(esn)\n\tweatherAPIEnabled := wc.Enable\n\tweatherAPIKey := wc.Key\n"
     "\tweatherAPIUnit := wc.Unit\n\tweatherAPIProvider := wc.Provider\n", 1),
    ("func weatherParser(speechText string,", "func weatherParser(esn string, speechText string,", 1),
    (":= getWeather(apiLocation,", ":= getWeather(esn, apiLocation,", 1),
])
in_func("wirepod/ttr/intentparam.go", "func ParamChecker(", "weatherParser(speechText,", "weatherParser(botSerial, speechText,", 1)
edit("wirepod/ttr/intentparam.go", [
    ("weatherParser(\"what's the weather\", botLocation, botUnits)", "weatherParser(botSerial, \"what's the weather\", botLocation, botUnits)", 1),
    ("\t\tif vars.APIConfig.Weather.Enable {", "\t\tif vars.WeatherFor(botSerial).Enable {", 1),
])
# prehistoricParamChecker has no serial: it keeps the global settings.
in_func("wirepod/ttr/intentparam.go", "func prehistoricParamChecker(", "weatherParser(speechText,", "weatherParser(\"\", speechText,", 1)

# Knowledge graph (LLM): each request uses its robot's settings.
path = os.path.join(ROOT, "wirepod/ttr/kgsim.go")
with open(path, encoding="utf-8") as f:
    src = f.read()
for sig in ("func CreateAIReq(", "func StreamingKGSim("):
    start = src.find(sig)
    if start < 0:
        sys.exit("kgsim.go: no " + sig)
    brace = src.find("{\n", start) + 2
    end = src.find("\nfunc ", start + 1)
    end = len(src) if end < 0 else end
    body = src[brace:end].replace("vars.APIConfig.Knowledge", "kc")
    src = src[:brace] + "\tkc := vars.KnowledgeFor(esn)\n" + body + src[end:]
with open(path, "w", encoding="utf-8") as f:
    f.write(src)
print("patched wirepod/ttr/kgsim.go CreateAIReq, StreamingKGSim")

edit("wirepod/ttr/matchIntentSend.go", [
    ("!isIntentGraph && vars.APIConfig.Knowledge.IntentGraph &&", "!isIntentGraph && vars.KnowledgeFor(esn).IntentGraph &&", 1),
])
edit("wirepod/preqs/intent.go", [
    ("if vars.APIConfig.Knowledge.IntentGraph && vars.APIConfig.Knowledge.Enable {",
     "if kc := vars.KnowledgeFor(req.Device); kc.IntentGraph && kc.Enable {", 1),
])
edit("wirepod/preqs/intent_graph.go", [
    ("\t\tif vars.APIConfig.Knowledge.IntentGraph && vars.APIConfig.Knowledge.Enable {\n\t\t\tif vars.APIConfig.Knowledge.Provider == \"houndify\" {",
     "\t\tif kc := vars.KnowledgeFor(req.Device); kc.IntentGraph && kc.Enable {\n\t\t\tif kc.Provider == \"houndify\" {", 1),
])
edit("wirepod/preqs/knowledgegraph.go", [
    ("\tif vars.APIConfig.Knowledge.Enable && vars.APIConfig.Knowledge.Provider != \"houndify\" {\n\t\tstreamingKG(",
     "\tif kc := vars.KnowledgeFor(req.Device); kc.Enable && kc.Provider != \"houndify\" {\n\t\tstreamingKG(", 1),
    # Houndify: each robot's own client (houndify_perbot.go) instead of the one global HKGclient.
    ("\tif vars.APIConfig.Knowledge.Enable && vars.APIConfig.Knowledge.Provider == \"houndify\" {\n"
     "\t\tlogger.Println(\"Sending request to Houndify...\")\n\t\tserverResponse := StreamAudioToHoundify(req, HKGclient)",
     "\tif client := houndifyFor(req.Device); client != nil {\n"
     "\t\tlogger.Println(\"Sending request to Houndify...\")\n\t\tserverResponse := StreamAudioToHoundify(req, *client)", 1),
    ("\tif vars.APIConfig.Knowledge.Enable {\n\t\tif vars.APIConfig.Knowledge.Provider == \"houndify\" {\n\t\t\treturn houndifyKG(speechReq)",
     "\tif kc := vars.KnowledgeFor(speechReq.Device); kc.Enable {\n\t\tif kc.Provider == \"houndify\" {\n\t\t\treturn houndifyKG(speechReq)", 1),
    ("\tif !vars.APIConfig.Knowledge.Enable || vars.APIConfig.Knowledge.Provider != \"houndify\" {\n\t\treturn \"Houndify is not enabled.\"\n\t}",
     "\tclient := houndifyFor(device)\n\tif client == nil {\n\t\treturn \"Houndify is not enabled.\"\n\t}", 1),
    ("serverResponse, err := HKGclient.TextSearch(req)", "serverResponse, err := client.TextSearch(req)", 1),
    # What Vector says when the owner hasn't picked a provider: settings live in the app now, not the web page.
    ("\"Knowledge graph is not enabled. This can be enabled in the web interface.\"",
     "\"Knowledge graph is not enabled. This can be configured in the Vector app.\"", 1),
])
edit("wirepod/ttr/intentparam.go", [
    ("KGSim(botSerial, \"The weather API is not configured.\")",
     "KGSim(botSerial, \"Weather is not enabled. This can be configured in the Vector app.\")", 1),
])
# The cert is loaded from a hard-coded ../certs; use the path vars.Init set (the same ../certs unless packaged).
edit("initwirepod/startserver.go", [
    ("certPub, _ = os.ReadFile(\"../certs/cert.crt\")", "certPub, _ = os.ReadFile(vars.CertPath)", 1),
    ("certPriv, err = os.ReadFile(\"../certs/cert.key\")", "certPriv, err = os.ReadFile(vars.KeyPath)", 1),
    # Offer HTTP/2 in ALPN. Robots send gRPC regardless, but proxies (Cloudflare's tunnel) only speak HTTP/2 to an
    # origin that offers it, and fall back to HTTP/1.1, which cmux hands to the REST handler instead of gRPC.
    ("\t\t\tCipherSuites: nil,\n", "\t\t\tCipherSuites: nil,\n\t\t\tNextProtos:   []string{\"h2\", \"http/1.1\"},\n", 2),
])
# Behind the Cloudflare tunnel, see each robot's own address (Cf-Connecting-Ip) instead of 127.0.0.1 (realpeer.go).
edit("servers/token/token.go", [("p, _ := peer.FromContext(ctx)", "p, _ := peer.FromContext(vars.WithRealPeer(ctx))", 2)])
edit("servers/jdocs/server.go", [("p, _ := peer.FromContext(ctx)", "p, _ := peer.FromContext(vars.WithRealPeer(ctx))", 2)])
edit("servers/jdocs/botInfoStorer.go", [("p, _ := peer.FromContext(ctx)", "p, _ := peer.FromContext(vars.WithRealPeer(ctx))", 1)])
# Connection check: robots on slower internet connections time out before all of the check's audio frames arrive
# (they got 7-42 frames), and then say they can't reach the server. A few frames prove the connection works.
edit("servers/chipper/connectioncheck.go", [
    ("\t\t\tif frames >= framesPerRequest {", "\t\t\tif frames >= framesPerRequest || frames >= 5 {", 1),
])
# Weather location: the one the owner set on the community server (per-bot file) wins over the robot's own setting.
edit("wirepod/ttr/intentparam.go", [
    ("\t\t\tbotLocation = robotSettings.DefaultLocation\n",
     "\t\t\tbotLocation = robotSettings.DefaultLocation\n"
     "\t\t\tif l := vars.LocationFor(botSerial); l != \"\" {\n\t\t\t\tbotLocation = l\n\t\t\t}\n", 2),
])
print("done")
