package petlibro

import (
	"encoding/base64"
	"errors"
	"io"
	"net"
	"testing"
	"time"
)

type fakeCameraTransport struct {
	frames chan *Packet
	done   chan struct{}
	closed bool
	closes int
}

func newFakeCameraTransport() *fakeCameraTransport {
	return &fakeCameraTransport{frames: make(chan *Packet, 16), done: make(chan struct{})}
}

func (f *fakeCameraTransport) packets() <-chan *Packet          { return f.frames }
func (f *fakeCameraTransport) doneSignal() <-chan struct{}      { return f.done }
func (f *fakeCameraTransport) RemoteAddr() net.Addr             { return &net.UDPAddr{} }
func (f *fakeCameraTransport) Protocol() string                 { return "test" }
func (f *fakeCameraTransport) SessionID() string                { return "ps-test" }
func (f *fakeCameraTransport) healthSnapshot() countersSnapshot { return countersSnapshot{} }
func (f *fakeCameraTransport) Close() error {
	f.closes++
	if !f.closed {
		close(f.done)
		f.closed = true
	}
	return nil
}

func testAccessUnit(t *testing.T, encodedSPS string, idr bool, cameraMS uint32) []byte {
	t.Helper()
	sps, err := base64.StdEncoding.DecodeString(encodedSPS)
	if err != nil {
		t.Fatal(err)
	}
	au := append([]byte{0, 0, 0, 1}, sps...)
	au = append(au, 0, 0, 0, 1, 0x68, 0, 0, 0)
	if idr {
		au = append(au, 0, 0, 0, 1, 0x65, byte(cameraMS))
	} else {
		au = append(au, 0, 0, 0, 1, 0x41, byte(cameraMS))
	}
	return au
}

func testInterPacket(marker byte, cameraMS uint32) *Packet {
	return &Packet{
		Codec: CodecH264, Payload: []byte{0, 0, 0, 1, 0x41, marker},
		CameraTimeMS: cameraMS, HasCameraTime: true,
	}
}

func testIDRWithoutConfig(marker byte, cameraMS uint32) *Packet {
	return &Packet{
		Codec: CodecH264, Payload: []byte{0, 0, 0, 1, 0x65, marker}, IsKeyframe: true,
		CameraTimeMS: cameraMS, HasCameraTime: true,
	}
}

func startCameraPrepare(camera *Camera, probeTimeout, hdWait time.Duration) <-chan error {
	result := make(chan error, 1)
	go func() {
		result <- camera.prepare(probeTimeout, hdWait)
	}()
	return result
}

func awaitPrepare(t *testing.T, result <-chan error) error {
	t.Helper()
	select {
	case err := <-result:
		return err
	case <-time.After(time.Second):
		t.Fatal("camera preparation did not finish")
		return nil
	}
}

func TestParseCameraOptions(t *testing.T) {
	for _, test := range []struct {
		raw     string
		quality string
		audio   bool
	}{
		{raw: "petlibro://camera?uid=123", quality: "hd"},
		{raw: "petlibro://camera?uid=123&quality=sd&audio=1", quality: "sd", audio: true},
		{raw: "petlibro://camera?quality=hd&send_delay_ctrl=0&hd_probe_wait_ms=0&streamctrl_variant=none&streamctrl_quality=2", quality: "hd"},
	} {
		opts, err := parseCameraOptions(test.raw)
		if err != nil {
			t.Fatal(err)
		}
		if opts.quality != test.quality || opts.audio != test.audio {
			t.Errorf("options=%+v, want quality=%q audio=%t", opts, test.quality, test.audio)
		}
	}
	if _, err := parseCameraOptions("petlibro://camera?quality=auto"); err == nil {
		t.Fatal("invalid camera quality was accepted")
	}
}

func TestCameraHDStartupSelectsHigherResolutionKeyframe(t *testing.T) {
	transport := newFakeCameraTransport()
	low := testAccessUnit(t, "Z2QAFqwa0BQF/yzcBAQFAAADAAEAAAMAHo8UIqA=", true, 100)
	high := testAccessUnit(t, "R00AKZmgHgCJ+WEAAAMD6AAATiCE", true, 200)
	inter := testInterPacket(1, 240)
	transport.frames <- &Packet{Codec: CodecH264, Payload: low, IsKeyframe: true, CameraTimeMS: 100, HasCameraTime: true}
	transport.frames <- &Packet{Codec: CodecH264, Payload: high, IsKeyframe: true, CameraTimeMS: 200, HasCameraTime: true}
	transport.frames <- inter

	camera := newCamera(transport, "hd", false, nil)
	if err := camera.prepare(time.Second, time.Second); err != nil {
		t.Fatal(err)
	}
	if camera.desc.Width != 1920 || camera.desc.Height != 1080 {
		t.Fatalf("selected=%dx%d, want 1920x1080", camera.desc.Width, camera.desc.Height)
	}
	unit, err := camera.Read()
	if err != nil {
		t.Fatal(err)
	}
	if unit.Codec != MediaH264 || !unit.Keyframe || string(unit.Payload) != string(high) || unit.Timestamp != 0 {
		t.Fatalf("first unit key=%t timestamp=%d size=%d", unit.Keyframe, unit.Timestamp, len(unit.Payload))
	}
	unit, err = camera.Read()
	if err != nil {
		t.Fatal(err)
	}
	if unit.Keyframe || string(unit.Payload) != string(inter.Payload) || unit.Timestamp != 3600 {
		t.Fatalf("second unit key=%t timestamp=%d payload=% x", unit.Keyframe, unit.Timestamp, unit.Payload)
	}
}

func TestCameraHDStartupTimeoutResynchronizesAtContinuousGOP(t *testing.T) {
	transport := newFakeCameraTransport()
	first := testAccessUnit(t, "Z2QAFqwa0BQF/yzcBAQFAAADAAEAAAMAHo8UIqA=", true, 100)
	second := testAccessUnit(t, "Z2QAFqwa0BQF/yzcBAQFAAADAAEAAAMAHo8UIqA=", true, 200)
	newestBeforeTimeout := testAccessUnit(t, "Z2QAFqwa0BQF/yzcBAQFAAADAAEAAAMAHo8UIqA=", true, 300)
	third := testAccessUnit(t, "Z2QAFqwa0BQF/yzcBAQFAAADAAEAAAMAHo8UIqA=", true, 1000)
	transport.frames <- &Packet{Codec: CodecH264, Payload: first, IsKeyframe: true, CameraTimeMS: 100, HasCameraTime: true}
	transport.frames <- testInterPacket(1, 140)
	transport.frames <- &Packet{Codec: CodecH264, Payload: second, IsKeyframe: true, CameraTimeMS: 200, HasCameraTime: true}
	transport.frames <- testInterPacket(2, 240)
	transport.frames <- &Packet{Codec: CodecH264, Payload: newestBeforeTimeout, IsKeyframe: true, CameraTimeMS: 300, HasCameraTime: true}
	transport.frames <- testInterPacket('A', 340)
	transport.frames <- testInterPacket('B', 380)
	transport.frames <- testInterPacket('C', 420)

	camera := newCamera(transport, "hd", false, nil)
	result := startCameraPrepare(camera, 500*time.Millisecond, 10*time.Millisecond)
	// Let the adapter consume A/B/C and process the stabilization timeout
	// before exposing the post-timeout live edge.
	time.Sleep(50 * time.Millisecond)
	transport.frames <- testInterPacket('D', 920)
	transport.frames <- testInterPacket('E', 960)
	transport.frames <- &Packet{Codec: CodecH264, Payload: third, IsKeyframe: true, CameraTimeMS: 1000, HasCameraTime: true}
	transport.frames <- testInterPacket('F', 1040)
	transport.frames <- testInterPacket('G', 1080)
	if err := awaitPrepare(t, result); err != nil {
		t.Fatal(err)
	}

	want := []struct {
		payload   []byte
		keyframe  bool
		timestamp uint32
	}{
		{payload: third, keyframe: true, timestamp: 0},
		{payload: testInterPacket('F', 1040).Payload, timestamp: 3600},
		{payload: testInterPacket('G', 1080).Payload, timestamp: 7200},
	}
	for i, expected := range want {
		unit, err := camera.Read()
		if err != nil {
			t.Fatal(err)
		}
		if unit.Keyframe != expected.keyframe || string(unit.Payload) != string(expected.payload) || unit.Timestamp != expected.timestamp {
			t.Fatalf("unit %d key=%t timestamp=%d payload=% x", i, unit.Keyframe, unit.Timestamp, unit.Payload)
		}
	}
}

func TestCameraHDStartupTimeoutWithoutNewerCandidateWaitsForLiveGOP(t *testing.T) {
	transport := newFakeCameraTransport()
	first := testAccessUnit(t, "Z2QAFqwa0BQF/yzcBAQFAAADAAEAAAMAHo8UIqA=", true, 100)
	live := testAccessUnit(t, "Z2QAFqwa0BQF/yzcBAQFAAADAAEAAAMAHo8UIqA=", true, 500)
	transport.frames <- &Packet{Codec: CodecH264, Payload: first, IsKeyframe: true, CameraTimeMS: 100, HasCameraTime: true}
	transport.frames <- testIDRWithoutConfig(0, 120)
	transport.frames <- testInterPacket('A', 140)

	camera := newCamera(transport, "hd", false, nil)
	result := startCameraPrepare(camera, 500*time.Millisecond, 10*time.Millisecond)
	time.Sleep(50 * time.Millisecond)
	transport.frames <- testInterPacket('D', 420)
	transport.frames <- testIDRWithoutConfig(1, 460)
	transport.frames <- &Packet{Codec: CodecH264, Payload: live, IsKeyframe: true, CameraTimeMS: 500, HasCameraTime: true}
	transport.frames <- testInterPacket('F', 540)
	if err := awaitPrepare(t, result); err != nil {
		t.Fatal(err)
	}

	firstUnit, err := camera.Read()
	if err != nil {
		t.Fatal(err)
	}
	secondUnit, err := camera.Read()
	if err != nil {
		t.Fatal(err)
	}
	if !firstUnit.Keyframe || string(firstUnit.Payload) != string(live) || firstUnit.Timestamp != 0 {
		t.Fatalf("first unit key=%t timestamp=%d payload=% x", firstUnit.Keyframe, firstUnit.Timestamp, firstUnit.Payload)
	}
	if secondUnit.Keyframe || string(secondUnit.Payload) != string(testInterPacket('F', 540).Payload) || secondUnit.Timestamp != 3600 {
		t.Fatalf("second unit key=%t timestamp=%d payload=% x", secondUnit.Keyframe, secondUnit.Timestamp, secondUnit.Payload)
	}
}

func TestCameraHDStartupEOFWhileWaitingForContinuousGOP(t *testing.T) {
	transport := newFakeCameraTransport()
	first := testAccessUnit(t, "Z2QAFqwa0BQF/yzcBAQFAAADAAEAAAMAHo8UIqA=", true, 100)
	transport.frames <- &Packet{Codec: CodecH264, Payload: first, IsKeyframe: true, CameraTimeMS: 100, HasCameraTime: true}
	transport.frames <- testInterPacket('A', 140)

	camera := newCamera(transport, "hd", false, nil)
	result := startCameraPrepare(camera, 500*time.Millisecond, 10*time.Millisecond)
	time.Sleep(50 * time.Millisecond)
	if err := transport.Close(); err != nil {
		t.Fatal(err)
	}
	if err := awaitPrepare(t, result); !errors.Is(err, io.EOF) {
		t.Fatalf("prepare error=%v, want EOF", err)
	}
}

func TestCameraHDStartupResyncPreservesOptionalAudio(t *testing.T) {
	transport := newFakeCameraTransport()
	first := testAccessUnit(t, "Z2QAFqwa0BQF/yzcBAQFAAADAAEAAAMAHo8UIqA=", true, 100)
	live := testAccessUnit(t, "Z2QAFqwa0BQF/yzcBAQFAAADAAEAAAMAHo8UIqA=", true, 500)
	adts := []byte{0xff, 0xf1, 0x50, 0x80, 0x01, 0x1f, 0xfc, 0x00}
	transport.frames <- &Packet{Codec: CodecH264, Payload: first, IsKeyframe: true, CameraTimeMS: 100, HasCameraTime: true}
	transport.frames <- testInterPacket('A', 140)
	transport.frames <- &Packet{Codec: CodecAACADTS, Payload: adts}

	camera := newCamera(transport, "hd", true, nil)
	result := startCameraPrepare(camera, 500*time.Millisecond, 10*time.Millisecond)
	time.Sleep(50 * time.Millisecond)
	transport.frames <- testInterPacket('D', 420)
	transport.frames <- &Packet{Codec: CodecH264, Payload: live, IsKeyframe: true, CameraTimeMS: 500, HasCameraTime: true}
	transport.frames <- &Packet{Codec: CodecAACADTS, Payload: adts}
	transport.frames <- testInterPacket('F', 540)
	if err := awaitPrepare(t, result); err != nil {
		t.Fatal(err)
	}

	video, err := camera.Read()
	if err != nil {
		t.Fatal(err)
	}
	audio, err := camera.Read()
	if err != nil {
		t.Fatal(err)
	}
	inter, err := camera.Read()
	if err != nil {
		t.Fatal(err)
	}
	if !video.Keyframe || string(video.Payload) != string(live) || video.Timestamp != 0 {
		t.Fatalf("video key=%t timestamp=%d payload=% x", video.Keyframe, video.Timestamp, video.Payload)
	}
	if audio.Codec != MediaAAC || audio.Timestamp != 0 {
		t.Fatalf("audio codec=%d timestamp=%d", audio.Codec, audio.Timestamp)
	}
	if inter.Keyframe || string(inter.Payload) != string(testInterPacket('F', 540).Payload) || inter.Timestamp != 3600 {
		t.Fatalf("inter key=%t timestamp=%d payload=% x", inter.Keyframe, inter.Timestamp, inter.Payload)
	}
}

func TestCameraCloseIsIdempotent(t *testing.T) {
	transport := newFakeCameraTransport()
	camera := newCamera(transport, "hd", false, nil)
	if err := camera.Close(); err != nil {
		t.Fatal(err)
	}
	if err := camera.Close(); err != nil {
		t.Fatal(err)
	}
	if transport.closes != 1 {
		t.Fatalf("transport closes=%d, want 1", transport.closes)
	}
}

func TestCameraNormalizesVideoClockAndMissingTimestamp(t *testing.T) {
	transport := newFakeCameraTransport()
	key := testAccessUnit(t, "Z2QAFqwa0BQF/yzcBAQFAAADAAEAAAMAHo8UIqA=", true, 100)
	transport.frames <- &Packet{Codec: CodecH264, Payload: key, IsKeyframe: true, CameraTimeMS: 100, HasCameraTime: true}
	camera := newCamera(transport, "sd", false, nil)
	if err := camera.prepare(time.Second, time.Second); err != nil {
		t.Fatal(err)
	}
	transport.frames <- &Packet{Codec: CodecH264, Payload: []byte{0, 0, 0, 1, 0x41}, CameraTimeMS: 200, HasCameraTime: true}
	transport.frames <- &Packet{Codec: CodecH264, Payload: []byte{0, 0, 0, 1, 0x41}}

	want := []uint32{0, 9000, 9001}
	for i, timestamp := range want {
		unit, err := camera.Read()
		if err != nil {
			t.Fatal(err)
		}
		if unit.Timestamp != timestamp {
			t.Fatalf("unit %d timestamp=%d, want %d", i, unit.Timestamp, timestamp)
		}
	}
}

func TestCameraClockRolloverAndDiscontinuities(t *testing.T) {
	t.Run("source rollover", func(t *testing.T) {
		var clock cameraClock
		if got := clock.normalize(^uint32(0)-5, true); got != 0 {
			t.Fatalf("first=%d, want 0", got)
		}
		if got := clock.normalize(4, true); got != 900 {
			t.Fatalf("after rollover=%d, want 900", got)
		}
	})

	t.Run("RTP rollover", func(t *testing.T) {
		var clock cameraClock
		before := uint32(47_721_858)
		if got := clock.normalize(0, true); got != 0 {
			t.Fatalf("first=%d, want 0", got)
		}
		wantBefore := uint32(uint64(before) * 90)
		if got := clock.normalize(before, true); got != wantBefore {
			t.Fatalf("before rollover=%d, want %d", got, wantBefore)
		}
		after := before + 1
		wantAfter := uint32(uint64(after) * 90)
		if got := clock.normalize(after, true); got != wantAfter {
			t.Fatalf("after rollover=%d, want %d", got, wantAfter)
		}
	})

	t.Run("repeat missing and backwards", func(t *testing.T) {
		var clock cameraClock
		steps := []struct {
			raw   uint32
			valid bool
			want  uint32
		}{
			{raw: 1000, valid: true, want: 0},
			{raw: 1100, valid: true, want: 9000},
			{raw: 1100, valid: true, want: 9001},
			{valid: false, want: 9002},
			{raw: 1050, valid: true, want: 9003},
			{raw: 1090, valid: true, want: 12603},
		}
		for i, step := range steps {
			if got := clock.normalize(step.raw, step.valid); got != step.want {
				t.Fatalf("step %d=%d, want %d", i, got, step.want)
			}
		}
	})

	t.Run("valid timestamp supersedes synthesized tick", func(t *testing.T) {
		var clock cameraClock
		if got := clock.normalize(100, true); got != 0 {
			t.Fatalf("first=%d, want 0", got)
		}
		if got := clock.normalize(0, false); got != 1 {
			t.Fatalf("missing=%d, want 1", got)
		}
		if got := clock.normalize(200, true); got != 9000 {
			t.Fatalf("resumed=%d, want 9000", got)
		}
	})
}

func TestCameraRequiresKeyframeForReadiness(t *testing.T) {
	transport := newFakeCameraTransport()
	inter := testAccessUnit(t, "Z2QAFqwa0BQF/yzcBAQFAAADAAEAAAMAHo8UIqA=", false, 100)
	key := testAccessUnit(t, "Z2QAFqwa0BQF/yzcBAQFAAADAAEAAAMAHo8UIqA=", true, 200)
	transport.frames <- &Packet{Codec: CodecH264, Payload: inter, CameraTimeMS: 100, HasCameraTime: true}
	transport.frames <- &Packet{Codec: CodecH264, Payload: key, IsKeyframe: true, CameraTimeMS: 200, HasCameraTime: true}

	camera := newCamera(transport, "sd", false, nil)
	if err := camera.prepare(time.Second, time.Second); err != nil {
		t.Fatal(err)
	}
	unit, err := camera.Read()
	if err != nil {
		t.Fatal(err)
	}
	if !unit.Keyframe || string(unit.Payload) != string(key) {
		t.Fatal("camera did not publish the first SPS-bearing keyframe")
	}
}

func TestCameraWaitsForRequestedAudioDescription(t *testing.T) {
	transport := newFakeCameraTransport()
	key := testAccessUnit(t, "Z2QAFqwa0BQF/yzcBAQFAAADAAEAAAMAHo8UIqA=", true, 100)
	adts := []byte{0xff, 0xf1, 0x50, 0x80, 0x01, 0x1f, 0xfc, 0x00}
	transport.frames <- &Packet{Codec: CodecH264, Payload: key, IsKeyframe: true}
	transport.frames <- &Packet{Codec: CodecAACADTS, Payload: adts}

	camera := newCamera(transport, "sd", true, nil)
	if err := camera.prepare(time.Second, time.Second); err != nil {
		t.Fatal(err)
	}
	if got := camera.Description().AudioConfig; string(got) != string(adts) {
		t.Fatalf("audio config=% x, want % x", got, adts)
	}
}

func TestCameraAudioTimestampsUseNativeAACClock(t *testing.T) {
	transport := newFakeCameraTransport()
	key := testAccessUnit(t, "Z2QAFqwa0BQF/yzcBAQFAAADAAEAAAMAHo8UIqA=", true, 100)
	// Sampling-frequency index 3 is 48 kHz. The observed PLAF203 stream is
	// 44.1 kHz; this alternate valid ADTS rate catches accidental 44.1 kHz
	// scaling at the normalized camera boundary.
	adts48k := []byte{0xff, 0xf1, 0x4c, 0x80, 0x01, 0x1f, 0xfc, 0x00}
	transport.frames <- &Packet{Codec: CodecH264, Payload: key, IsKeyframe: true}
	transport.frames <- &Packet{Codec: CodecAACADTS, Payload: adts48k}

	camera := newCamera(transport, "sd", true, nil)
	if err := camera.prepare(time.Second, time.Second); err != nil {
		t.Fatal(err)
	}
	if _, err := camera.Read(); err != nil { // buffered video readiness unit
		t.Fatal(err)
	}
	transport.frames <- &Packet{Codec: CodecAACADTS, Payload: adts48k}
	transport.frames <- &Packet{Codec: CodecAACADTS, Payload: adts48k}
	for i, want := range []uint32{0, 1024} {
		unit, err := camera.Read()
		if err != nil {
			t.Fatal(err)
		}
		if unit.Codec != MediaAAC || unit.Timestamp != want {
			t.Fatalf("audio unit %d codec=%d timestamp=%d, want AAC/%d", i, unit.Codec, unit.Timestamp, want)
		}
	}
}
