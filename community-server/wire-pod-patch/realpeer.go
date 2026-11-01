package vars

import (
	"context"
	"net"

	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/peer"
)

// WithRealPeer returns ctx with the robot's own address as its gRPC peer. Behind the Cloudflare tunnel every robot
// connects from cloudflared on this machine (127.0.0.1); the tunnel passes the robot's address in Cf-Connecting-Ip.
// wire-pod uses the peer address to pair a robot's token request with its later jdocs requests, so without this all
// robots would share 127.0.0.1. Direct (non-tunnel) connections are left as they are.
func WithRealPeer(ctx context.Context) context.Context {
	p, ok := peer.FromContext(ctx)
	if !ok || p.Addr == nil {
		return ctx
	}
	host, _, err := net.SplitHostPort(p.Addr.String())
	if err != nil || !net.ParseIP(host).IsLoopback() {
		return ctx
	}
	md, _ := metadata.FromIncomingContext(ctx)
	vals := md.Get("cf-connecting-ip")
	if len(vals) == 0 {
		return ctx
	}
	ip := net.ParseIP(vals[0])
	if ip == nil {
		return ctx
	}
	// wire-pod takes the text before the first ':' as the address, so an IPv6 address is shortened to a key that
	// still tells robots apart well enough for pairing (and isn't reachable anyway).
	return peer.NewContext(ctx, &peer.Peer{Addr: &net.TCPAddr{IP: ip}, AuthInfo: p.AuthInfo})
}
