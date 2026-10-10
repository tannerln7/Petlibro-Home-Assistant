package petlibro

import (
	"crypto/rand"
	"encoding/binary"
	"time"
)

// handshake runs the LAN-side P2P handshake: LAN_SEARCH3 (w3=1, w3=2),
// KNOCK2, LOGIN A+B, then waits for LOGIN_RESP.  Returns nil when the
// camera has acknowledged the session and is ready for the bootstrap
// IOCtrl sequence.
//
// petlibro divergence vs pkg/tutk: pkg/tutk has no LAN_SEARCH /
// KNOCK at all — it goes through Nebula (relay-server bootstrap in
// connectDirect/connectRemote, see pkg/tutk/conn.go:32) and never
// performs a direct-LAN UDP probe.  Petlibro is a non-Nebula direct-LAN
// variant: the Petlibro Android app skips Kalay's relay and addresses
// the camera by IP after a LAN_SEARCH3/KNOCK2 handshake.  Both opcodes
// (msgLANSearch3=0x0601, msgKnock2=0x0402) and their reply forms are
// petlibro-only — there is no analog in pkg/tutk to consolidate with.
func (c *Client) handshake() error {
	logRecv := func(label string, pkt []byte) {
		if !c.verbose || len(pkt) < 12 {
			return
		}
		mt := binary.LittleEndian.Uint16(pkt[8:])
		flag := byte(0)
		if len(pkt) > 3 {
			flag = pkt[3]
		}
		if len(pkt) >= 0x1C+0x1A {
			inner := pkt[0x1C:]
			log.Trace().Str("physical_session_id", c.sessionID).Str("phase", label).
				Uint16("message_type", mt).Uint8("flags", flag).Int("packet_length", len(pkt)).
				Uint8("inner_type", inner[0]).Uint8("inner_subtype", inner[1]).
				Uint8("inner_status", inner[0x18]).Msg("petlibro handshake packet received")
		} else {
			log.Trace().Str("physical_session_id", c.sessionID).Str("phase", label).
				Uint16("message_type", mt).Uint8("flags", flag).Int("packet_length", len(pkt)).
				Msg("petlibro handshake packet received")
		}
	}

	if err := c.send(buildLANSearch3(c.uid, c.nonce, 1)); err != nil {
		return err
	}
	gotLANSearchR := false
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		pkt, err := c.recvOne(500 * time.Millisecond)
		if err != nil || len(pkt) < 12 {
			continue
		}
		logRecv("after-LAN_SEARCH3-w3=1", pkt)
		if binary.LittleEndian.Uint16(pkt[8:]) == msgLANSearchR {
			gotLANSearchR = true
			break
		}
	}
	if c.verbose && !gotLANSearchR {
		log.Debug().Str("physical_session_id", c.sessionID).
			Msg("petlibro handshake continued without optional LAN_SEARCH response")
	}

	_ = c.send(buildLANSearch3(c.uid, c.nonce, 2))
	time.Sleep(30 * time.Millisecond)
	_ = c.send(buildKnock2(c.uid, c.nonce))
	gotKnockRR2 := false
	deadline = time.Now().Add(1500 * time.Millisecond)
	for time.Now().Before(deadline) {
		pkt, err := c.recvOne(500 * time.Millisecond)
		if err != nil || len(pkt) < 12 {
			continue
		}
		logRecv("after-KNOCK2", pkt)
		if binary.LittleEndian.Uint16(pkt[8:]) == msgKnockRR2 {
			gotKnockRR2 = true
			break
		}
	}
	if c.verbose && !gotKnockRR2 {
		log.Debug().Str("physical_session_id", c.sessionID).
			Msg("petlibro handshake continued without optional KNOCK response")
	}

	// LOGIN A + LOGIN B (fresh random seed, B = seed+1).  Earlier
	// versions sent a "DTLS-shaped" 257-byte first packet here
	// (datatype=1, kseq=0) modelled on the official app's capture;
	// the camera turned out not to require it.  See
	// .omc/research/_dtls_skip_test/ for the empirical bisect that
	// resolved CONSOLIDATED.md Needs-user-input #3.
	//
	// petlibro divergence vs pkg/tutk: tutk's equivalent client-side
	// login is Session16.ClientStart (pkg/tutk/session16.go:80), which
	// builds a 598-byte buffer with username/password at fixed offsets
	// 24 and 281 and is sent in the clear inside a 0x07 control msg.
	// Petlibro's LOGIN pair (buildLoginPair) is two messages, padded to
	// 570/572 bytes, with a Luffy-encrypted trailer carrying auth_type/
	// opcode_support/enable_video_on_connect fields — and the second
	// adds enable_audio_on_connect.  Different wire shape and a second
	// AV-LOGIN_1 packet means we can't reuse Session16.ClientStart.
	var seedBytes [4]byte
	_, _ = rand.Read(seedBytes[:])
	seedBytes[0] &= 0xFE
	seed := binary.LittleEndian.Uint32(seedBytes[:])
	loginA, loginB := buildLoginPair(seed)
	c.dumpC2DInner(loginA)
	if err := c.send(buildOuter(c.nonce, 0, loginA, 0x00, 0x00, flagsSession)); err != nil {
		return err
	}
	c.dumpC2DInner(loginB)
	if err := c.send(buildOuter(c.nonce, 1, loginB, 0x00, 0x00, flagsSession)); err != nil {
		return err
	}

	// Wait for LOGIN_RESP — outer mt=0x408, inner[1]==0x21, inner[0x18]==0
	deadline = time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		pkt, err := c.recvOne(500 * time.Millisecond)
		if err != nil || len(pkt) < 0x1C+0x1A {
			continue
		}
		logRecv("after-LOGIN", pkt)
		if binary.LittleEndian.Uint16(pkt[8:]) != msgSessionD2C {
			continue
		}
		inner := pkt[0x1C:]
		if inner[1] == 0x21 && inner[0x18] == 0 {
			return nil
		}
	}
	return errLoginResponseTimeout
}
