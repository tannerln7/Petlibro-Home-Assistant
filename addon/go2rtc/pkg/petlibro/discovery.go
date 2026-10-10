package petlibro

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/creds"
	"github.com/AlexxIT/go2rtc/pkg/tutk"
)

var discoveryCache sync.Map

const (
	defaultDiscoveryTimeout      = 15 * time.Second
	defaultBroadcastDuration     = 750 * time.Millisecond
	defaultMaxUnicastPerSecond   = 32
	defaultCandidateResponseWait = 250 * time.Millisecond
	defaultPostScanResponseWait  = 300 * time.Millisecond
	maxAutomaticDiscoveryTargets = 1024
)

type ResolveOptions struct {
	UID                 string
	Subnets             []string
	CachedIP            net.IP
	Candidates          []net.IP
	Timeout             time.Duration
	BroadcastDuration   time.Duration
	MaxUnicastPerSecond int
	Verbose             bool
}

type ResolveStats struct {
	LANSearchW3_1Sent     uint64 `json:"lan_search_w3_1_sent"`
	LANSearchW3_2Sent     uint64 `json:"lan_search_w3_2_sent"`
	Knock2Sent            uint64 `json:"knock2_sent"`
	LANSearchRReceived    uint64 `json:"lan_search_r_received"`
	KnockRR2Received      uint64 `json:"knock_rr2_received"`
	WrongUIDRejected      uint64 `json:"wrong_uid_rejected"`
	NonceMismatchRejected uint64 `json:"nonce_mismatch_rejected"`
	BroadcastsSent        uint64 `json:"broadcasts_sent"`
	UnicastsSent          uint64 `json:"unicasts_sent"`
	PacketsReceived       uint64 `json:"packets_received"`
	ResponsesRejected     uint64 `json:"responses_rejected"`
	SendErrors            uint64 `json:"send_errors"`
	DeadlineExceeded      bool   `json:"deadline_exceeded"`
}

type ResolveResult struct {
	UID       string       `json:"uid"`
	Subnet    string       `json:"subnet,omitempty"`
	Resolved  bool         `json:"resolved"`
	IPAddress *string      `json:"ip_address"`
	Method    string       `json:"method"`
	ElapsedMS int64        `json:"elapsed_ms"`
	ErrorCode string       `json:"error_code,omitempty"`
	Error     string       `json:"error,omitempty"`
	Stats     ResolveStats `json:"stats"`
}

type ResolveError struct {
	Code string
	Err  error
}

func (e *ResolveError) Error() string { return e.Err.Error() }
func (e *ResolveError) Unwrap() error { return e.Err }

// ResolveUID preserves the original small API while using the bounded staged
// resolver. New callers should use ResolveUIDContext to retain diagnostics.
func ResolveUID(uid string, subnets []string, timeout time.Duration) (net.IP, error) {
	result, err := ResolveUIDContext(context.Background(), ResolveOptions{
		UID:     uid,
		Subnets: subnets,
		Timeout: timeout,
	})
	if err != nil {
		return nil, err
	}
	return net.ParseIP(*result.IPAddress), nil
}

// ResolveUIDContext finds a Petlibro camera using a cached target, broadcast,
// known candidates, and finally one paced subnet sweep. One absolute deadline
// covers target construction, reads, and every write.
func ResolveUIDContext(parent context.Context, options ResolveOptions) (ResolveResult, error) {
	started := time.Now()
	result := ResolveResult{UID: options.UID, Method: "not_found"}
	if len(options.Subnets) == 1 {
		result.Subnet = options.Subnets[0]
	}

	if err := validateResolveOptions(&options); err != nil {
		result.ErrorCode = "invalid_request"
		result.Error = err.Error()
		result.ElapsedMS = time.Since(started).Milliseconds()
		return result, &ResolveError{Code: result.ErrorCode, Err: err}
	}

	ctx, cancel := context.WithTimeout(parent, options.Timeout)
	defer cancel()

	conn, err := net.ListenUDP("udp4", nil)
	if err != nil {
		err = fmt.Errorf("petlibro: discovery bind: %w", err)
		result.ErrorCode = "send_failed"
		result.Error = err.Error()
		result.ElapsedMS = time.Since(started).Milliseconds()
		return result, &ResolveError{Code: result.ErrorCode, Err: err}
	}
	defer conn.Close()

	nonce := make([]byte, 8)
	if _, err = rand.Read(nonce); err != nil {
		err = fmt.Errorf("petlibro: discovery nonce: %w", err)
		result.ErrorCode = "send_failed"
		result.Error = err.Error()
		result.ElapsedMS = time.Since(started).Milliseconds()
		return result, &ResolveError{Code: result.ErrorCode, Err: err}
	}

	plan := buildDiscoveryPlan(options)
	result, err = resolveWithPlan(ctx, conn, options, nonce, plan, started)
	return result, err
}

type udpDiscoveryConn interface {
	ReadFromUDP([]byte) (int, *net.UDPAddr, error)
	WriteToUDP([]byte, *net.UDPAddr) (int, error)
	SetDeadline(time.Time) error
	SetReadDeadline(time.Time) error
}

type discoveryPlan struct {
	cached     []*net.UDPAddr
	broadcasts []*net.UDPAddr
	candidates []*net.UDPAddr
	unicasts   []*net.UDPAddr
}

type resolveCounters struct {
	lanSearchW3_1Sent     atomic.Uint64
	lanSearchW3_2Sent     atomic.Uint64
	knock2Sent            atomic.Uint64
	lanSearchRReceived    atomic.Uint64
	knockRR2Received      atomic.Uint64
	wrongUIDRejected      atomic.Uint64
	nonceMismatchRejected atomic.Uint64
	broadcasts            atomic.Uint64
	unicasts              atomic.Uint64
	received              atomic.Uint64
	rejected              atomic.Uint64
	sendErrors            atomic.Uint64
}

type discoveryResponseReason uint8

const (
	discoveryResponseAccepted discoveryResponseReason = iota
	discoveryResponseMalformed
	discoveryResponseUnknown
	discoveryResponseWrongUID
	discoveryResponseNonceMismatch
)

type discoveredAddress struct {
	addr *net.UDPAddr
}

type discoveryProbe struct {
	search1 []byte
	search2 []byte
	knock2  []byte
	nonce   []byte
}

func newDiscoveryProbe(uid string, nonce []byte) discoveryProbe {
	return discoveryProbe{
		search1: tutk.TransCodePartial(nil, buildLANSearch3(uid, nonce, 1)),
		search2: tutk.TransCodePartial(nil, buildLANSearch3(uid, nonce, 2)),
		knock2:  tutk.TransCodePartial(nil, buildKnock2(uid, nonce)),
		nonce:   append([]byte(nil), nonce...),
	}
}

func validateResolveOptions(options *ResolveOptions) error {
	if len(options.UID) != 20 {
		return fmt.Errorf("petlibro: uid must be 20 chars (got %d)", len(options.UID))
	}
	if options.Timeout <= 0 {
		return fmt.Errorf("petlibro: discovery timeout must be positive")
	}
	for _, subnet := range options.Subnets {
		if _, network, err := net.ParseCIDR(subnet); err != nil || network.IP.To4() == nil {
			return fmt.Errorf("petlibro: invalid IPv4 subnet %q", subnet)
		}
	}
	if options.BroadcastDuration <= 0 {
		options.BroadcastDuration = defaultBroadcastDuration
	}
	if options.BroadcastDuration > options.Timeout {
		options.BroadcastDuration = options.Timeout
	}
	if options.MaxUnicastPerSecond <= 0 {
		options.MaxUnicastPerSecond = defaultMaxUnicastPerSecond
	} else if options.MaxUnicastPerSecond > 512 {
		return fmt.Errorf("petlibro: max unicast rate must not exceed 512")
	}
	return nil
}

func resolveWithPlan(ctx context.Context, conn udpDiscoveryConn, options ResolveOptions, nonce []byte, plan discoveryPlan, started time.Time) (ResolveResult, error) {
	result := ResolveResult{UID: options.UID, Method: "not_found"}
	if len(options.Subnets) == 1 {
		result.Subnet = options.Subnets[0]
	}
	deadline, ok := ctx.Deadline()
	if !ok {
		deadline = started.Add(options.Timeout)
	}
	_ = conn.SetDeadline(deadline)
	deadlineStop := make(chan struct{})
	deadlineDone := make(chan struct{})
	resolved := false
	defer func() {
		close(deadlineStop)
		<-deadlineDone
		if resolved {
			// discoverByUID reuses this socket for the camera handshake.
			_ = conn.SetDeadline(time.Time{})
		}
	}()
	go func() {
		defer close(deadlineDone)
		select {
		case <-ctx.Done():
			_ = conn.SetDeadline(time.Now())
		case <-deadlineStop:
		}
	}()

	probe := newDiscoveryProbe(options.UID, nonce)
	match := make(chan discoveredAddress, 1)
	readerDone := make(chan struct{})
	counters := &resolveCounters{}
	go receiveDiscoveryResponses(ctx, conn, options.UID, probe.nonce, match, readerDone, counters)

	finish := func(method string, addr *net.UDPAddr, cause error) (ResolveResult, error) {
		result.ElapsedMS = time.Since(started).Milliseconds()
		result.Stats = counters.snapshot(errors.Is(cause, context.DeadlineExceeded) || errors.Is(ctx.Err(), context.DeadlineExceeded))
		if addr != nil {
			resolved = true
			result.Method = method
			result.Resolved = true
			ipAddress := addr.IP.String()
			result.IPAddress = &ipAddress
			return result, nil
		}
		result.Method = "not_found"
		code := classifyResolveFailure(cause, result.Stats)
		result.ErrorCode = code
		if cause == nil {
			cause = fmt.Errorf("petlibro: camera with requested uid not found")
		}
		result.Error = cause.Error()
		return result, &ResolveError{Code: code, Err: cause}
	}

	waitFor := func(duration time.Duration) (*net.UDPAddr, error) {
		if duration <= 0 {
			select {
			case found := <-match:
				return found.addr, nil
			default:
				return nil, nil
			}
		}
		timer := time.NewTimer(duration)
		defer timer.Stop()
		select {
		case found := <-match:
			return found.addr, nil
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-timer.C:
			return nil, nil
		}
	}

	if addr, err := sendStage(ctx, conn, probe, plan.cached, false, defaultCandidateResponseWait, match, counters); addr != nil || err != nil {
		return finish("cached", addr, err)
	}

	broadcastUntil := time.Now().Add(options.BroadcastDuration)
	for round := 0; round < 3 && time.Now().Before(broadcastUntil); round++ {
		addr, err := sendStage(ctx, conn, probe, plan.broadcasts, true, 0, match, counters)
		if addr != nil || err != nil {
			return finish("broadcast", addr, err)
		}
		remaining := time.Until(broadcastUntil)
		if spacing := options.BroadcastDuration / 3; remaining > spacing {
			remaining = spacing
		}
		if addr, err = waitFor(remaining); addr != nil || err != nil {
			return finish("broadcast", addr, err)
		}
	}

	if addr, err := sendStage(ctx, conn, probe, plan.candidates, false, defaultCandidateResponseWait, match, counters); addr != nil || err != nil {
		return finish("candidate", addr, err)
	}

	interval := time.Second / time.Duration(options.MaxUnicastPerSecond)
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for _, target := range plan.unicasts {
		select {
		case found := <-match:
			return finish("unicast", found.addr, nil)
		case <-ctx.Done():
			return finish("not_found", nil, ctx.Err())
		case <-ticker.C:
		}
		// If a response and pacing tick became ready together, prefer the
		// completed discovery instead of starting one more target exchange.
		select {
		case found := <-match:
			return finish("unicast", found.addr, nil)
		case <-ctx.Done():
			return finish("not_found", nil, ctx.Err())
		default:
		}
		if err := sendDiscoveryProbe(ctx, conn, probe, target, false, counters); err != nil && ctx.Err() != nil {
			return finish("not_found", nil, ctx.Err())
		}
	}

	if addr, err := waitFor(minDuration(defaultPostScanResponseWait, time.Until(deadline))); addr != nil || err != nil {
		return finish("unicast", addr, err)
	}
	if time.Now().Before(deadline) {
		<-ctx.Done()
	}
	_ = conn.SetReadDeadline(time.Now())
	select {
	case <-readerDone:
	case <-time.After(50 * time.Millisecond):
	}
	return finish("not_found", nil, ctx.Err())
}

func sendStage(ctx context.Context, conn udpDiscoveryConn, probe discoveryProbe, targets []*net.UDPAddr, broadcast bool, wait time.Duration, match <-chan discoveredAddress, counters *resolveCounters) (*net.UDPAddr, error) {
	if len(targets) == 0 {
		return nil, nil
	}
	for _, target := range targets {
		select {
		case found := <-match:
			return found.addr, nil
		case <-ctx.Done():
			return nil, ctx.Err()
		default:
		}
		if err := sendDiscoveryProbe(ctx, conn, probe, target, broadcast, counters); err != nil && ctx.Err() != nil {
			return nil, ctx.Err()
		}
	}
	if wait <= 0 {
		return nil, nil
	}
	timer := time.NewTimer(wait)
	defer timer.Stop()
	select {
	case found := <-match:
		return found.addr, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-timer.C:
		return nil, nil
	}
}

func sendDiscoveryProbe(ctx context.Context, conn udpDiscoveryConn, probe discoveryProbe, target *net.UDPAddr, broadcast bool, counters *resolveCounters) error {
	sent := false
	var lastErr error
	packets := []struct {
		wire []byte
		sent *atomic.Uint64
	}{
		{wire: probe.search1, sent: &counters.lanSearchW3_1Sent},
		{wire: probe.search2, sent: &counters.lanSearchW3_2Sent},
		{wire: probe.knock2, sent: &counters.knock2Sent},
	}
	for i, packet := range packets {
		if err := ctx.Err(); err != nil {
			return err
		}
		if _, err := conn.WriteToUDP(packet.wire, target); err != nil {
			counters.sendErrors.Add(1)
			lastErr = err
		} else {
			sent = true
			packet.sent.Add(1)
		}
		// The direct camera handshake requires a short settle between the
		// second LAN_SEARCH3 leg and KNOCK2. Keep it cancellable so one
		// absolute resolver deadline still bounds every target.
		if i == 1 {
			timer := time.NewTimer(30 * time.Millisecond)
			select {
			case <-ctx.Done():
				timer.Stop()
				return ctx.Err()
			case <-timer.C:
			}
		}
	}
	if sent {
		if broadcast {
			counters.broadcasts.Add(1)
		} else {
			counters.unicasts.Add(1)
		}
	}
	return lastErr
}

func receiveDiscoveryResponses(ctx context.Context, conn udpDiscoveryConn, uid string, nonce []byte, match chan<- discoveredAddress, done chan<- struct{}, counters *resolveCounters) {
	defer close(done)
	buf := make([]byte, 65535)
	for {
		n, addr, err := conn.ReadFromUDP(buf)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			continue
		}
		if addr == nil || addr.IP.IsUnspecified() {
			counters.rejected.Add(1)
			continue
		}
		counters.received.Add(1)
		pkt := tutk.ReverseTransCodePartial(nil, append([]byte(nil), buf[:n]...))
		if len(pkt) >= 10 {
			switch binary.LittleEndian.Uint16(pkt[8:]) {
			case msgLANSearchR:
				counters.lanSearchRReceived.Add(1)
			case msgKnockRR2:
				counters.knockRR2Received.Add(1)
			}
		}
		if reason := classifyDiscoveryResponse(pkt, uid, nonce); reason != discoveryResponseAccepted {
			counters.rejected.Add(1)
			switch reason {
			case discoveryResponseWrongUID:
				counters.wrongUIDRejected.Add(1)
			case discoveryResponseNonceMismatch:
				counters.nonceMismatchRejected.Add(1)
			}
			continue
		}
		select {
		case match <- discoveredAddress{addr: &net.UDPAddr{IP: append(net.IP(nil), addr.IP...), Port: lanPort}}:
		case <-ctx.Done():
		}
		return
	}
}

func classifyDiscoveryResponse(pkt []byte, uid string, nonce []byte) discoveryResponseReason {
	if len(pkt) < 12 {
		return discoveryResponseMalformed
	}
	switch binary.LittleEndian.Uint16(pkt[8:]) {
	case msgLANSearchR:
		// A discovery response is only useful when it identifies the UID.
		// Do not accept anonymous broadcast replies from another camera.
		if len(pkt) < 0x24 {
			return discoveryResponseMalformed
		}
		candidate := bytes.Trim(pkt[0x10:0x24], "\x00")
		if len(candidate) != 20 || !isASCIIAlphaNumeric(candidate) {
			return discoveryResponseMalformed
		}
		if string(candidate) != uid {
			return discoveryResponseWrongUID
		}
		return discoveryResponseAccepted
	case msgKnockRR2:
		// PLAF203 firmware may not answer LAN_SEARCH3 at all. It does answer
		// the complete LAN_SEARCH3(w3=1,w3=2) + KNOCK2 exchange, echoing both
		// the requested UID and nonce in KNOCK_RR2.
		if len(pkt) < 0x2C {
			return discoveryResponseMalformed
		}
		if string(pkt[0x10:0x24]) != uid {
			return discoveryResponseWrongUID
		}
		if !bytes.Equal(pkt[0x24:0x2C], nonce) {
			return discoveryResponseNonceMismatch
		}
		return discoveryResponseAccepted
	default:
		return discoveryResponseUnknown
	}
}

func isASCIIAlphaNumeric(value []byte) bool {
	for _, b := range value {
		if !((b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z') || (b >= '0' && b <= '9')) {
			return false
		}
	}
	return true
}

func (c *resolveCounters) snapshot(deadlineExceeded bool) ResolveStats {
	return ResolveStats{
		LANSearchW3_1Sent: c.lanSearchW3_1Sent.Load(), LANSearchW3_2Sent: c.lanSearchW3_2Sent.Load(),
		Knock2Sent: c.knock2Sent.Load(), LANSearchRReceived: c.lanSearchRReceived.Load(),
		KnockRR2Received: c.knockRR2Received.Load(), WrongUIDRejected: c.wrongUIDRejected.Load(),
		NonceMismatchRejected: c.nonceMismatchRejected.Load(),
		BroadcastsSent:        c.broadcasts.Load(), UnicastsSent: c.unicasts.Load(),
		PacketsReceived: c.received.Load(), ResponsesRejected: c.rejected.Load(),
		SendErrors: c.sendErrors.Load(), DeadlineExceeded: deadlineExceeded,
	}
}

func classifyResolveFailure(cause error, stats ResolveStats) string {
	if stats.BroadcastsSent+stats.UnicastsSent == 0 && stats.SendErrors > 0 {
		return "send_failed"
	}
	if stats.PacketsReceived > 0 && stats.ResponsesRejected == stats.PacketsReceived {
		return "invalid_response"
	}
	if errors.Is(cause, context.DeadlineExceeded) {
		return "deadline_exceeded"
	}
	return "not_found"
}

func minDuration(a, b time.Duration) time.Duration {
	if a < b {
		return a
	}
	return b
}

// discoverByUID keeps Dial on the same socket so the subsequent handshake can
// reuse the ephemeral source port selected for discovery.
func discoverByUID(conn *net.UDPConn, uid string, nonce []byte, subnets []string, timeout time.Duration, verbose bool) (*net.UDPAddr, error) {
	options := ResolveOptions{UID: uid, Subnets: subnets, Timeout: timeout, Verbose: verbose}
	if err := validateResolveOptions(&options); err != nil {
		return nil, err
	}
	cacheKey := discoveryCacheKey(uid, subnets)
	if cached, ok := discoveryCache.Load(cacheKey); ok {
		options.CachedIP = net.ParseIP(cached.(string))
	}
	plan := buildDiscoveryPlan(options)
	ctx, cancel := context.WithTimeout(context.Background(), options.Timeout)
	defer cancel()
	result, err := resolveWithPlan(ctx, conn, options, nonce, plan, time.Now())
	if err != nil {
		if verbose {
			log.Debug().Str("method", result.Method).Int64("elapsed_ms", result.ElapsedMS).
				Interface("stats", result.Stats).Str("error_code", result.ErrorCode).
				Msg("petlibro discovery failed")
		}
		return nil, err
	}
	discoveryCache.Store(cacheKey, *result.IPAddress)
	// Discovery learns an endpoint that was not present in the source URL.
	// Register it once with the centralized log sanitizer before any component
	// emits it as structured context.
	creds.AddSecret(*result.IPAddress)
	if verbose {
		log.Debug().Str("method", result.Method).Str("ip", *result.IPAddress).
			Int64("elapsed_ms", result.ElapsedMS).Interface("stats", result.Stats).
			Msg("petlibro discovery resolved")
	}
	return &net.UDPAddr{IP: net.ParseIP(*result.IPAddress).To4(), Port: lanPort}, nil
}

func buildDiscoveryPlan(options ResolveOptions) discoveryPlan {
	seen := map[string]struct{}{}
	add := func(list *[]*net.UDPAddr, ip net.IP) {
		ip4 := ip.To4()
		if ip4 == nil || ip4.IsUnspecified() {
			return
		}
		key := ip4.String()
		if _, ok := seen[key]; ok {
			return
		}
		seen[key] = struct{}{}
		*list = append(*list, &net.UDPAddr{IP: append(net.IP(nil), ip4...), Port: lanPort})
	}

	var plan discoveryPlan
	add(&plan.cached, options.CachedIP)
	add(&plan.broadcasts, net.IPv4bcast)
	for _, subnet := range options.Subnets {
		if directed := cidrBroadcast(subnet); directed != nil {
			add(&plan.broadcasts, directed)
		}
	}
	for _, candidate := range options.Candidates {
		add(&plan.candidates, candidate)
	}
	for _, subnet := range options.Subnets {
		for _, host := range cidrDiscoveryTargets(subnet, 65536) {
			add(&plan.unicasts, host)
		}
	}

	ifaces, err := net.Interfaces()
	if err != nil {
		return plan
	}
	for _, iface := range ifaces {
		if iface.Flags&net.FlagUp == 0 || iface.Flags&net.FlagLoopback != 0 ||
			iface.Flags&net.FlagBroadcast == 0 || iface.Flags&net.FlagPointToPoint != 0 {
			continue
		}
		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, addr := range addrs {
			ipNet, ok := addr.(*net.IPNet)
			if !ok {
				continue
			}
			ip := ipNet.IP.To4()
			mask := ipNet.Mask
			if ip == nil || len(mask) != net.IPv4len {
				continue
			}
			add(&plan.broadcasts, broadcastIP(ip, mask))
			if len(options.Subnets) == 0 {
				for _, host := range subnetDiscoveryTargets(ip, mask, maxAutomaticDiscoveryTargets) {
					add(&plan.unicasts, host)
				}
			}
		}
	}
	return plan
}

func cidrBroadcast(subnet string) net.IP {
	_, ipNet, err := net.ParseCIDR(subnet)
	if err != nil {
		return nil
	}
	ip := ipNet.IP.To4()
	if ip == nil || len(ipNet.Mask) != net.IPv4len {
		return nil
	}
	return broadcastIP(ip, ipNet.Mask)
}

func broadcastIP(ip net.IP, mask net.IPMask) net.IP {
	return net.IPv4(ip[0]|^mask[0], ip[1]|^mask[1], ip[2]|^mask[2], ip[3]|^mask[3])
}

func discoveryCacheKey(uid string, subnets []string) string {
	return uid + "|" + strings.Join(subnets, ",")
}

func clearDiscoveryCache(uid string, subnets []string) {
	discoveryCache.Delete(discoveryCacheKey(uid, subnets))
}

func cidrDiscoveryTargets(subnet string, max int) []net.IP {
	_, ipNet, err := net.ParseCIDR(subnet)
	if err != nil {
		return nil
	}
	ip := ipNet.IP.To4()
	mask := ipNet.Mask
	if ip == nil || len(mask) != net.IPv4len {
		return nil
	}
	return subnetDiscoveryTargets(ip, mask, max)
}

func subnetDiscoveryTargets(ip net.IP, mask net.IPMask, max int) []net.IP {
	ones, bits := mask.Size()
	if bits != 8*net.IPv4len {
		return nil
	}
	total := 1 << (bits - ones)
	if total > max {
		return nil
	}
	base := binary.BigEndian.Uint32(ip) & binary.BigEndian.Uint32(mask)
	var targets []net.IP
	for i := uint32(1); i < uint32(total-1); i++ {
		host := make(net.IP, net.IPv4len)
		binary.BigEndian.PutUint32(host, base+i)
		if host.Equal(ip) {
			continue
		}
		targets = append(targets, host)
	}
	return targets
}
