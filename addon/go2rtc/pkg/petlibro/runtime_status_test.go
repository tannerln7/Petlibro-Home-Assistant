package petlibro

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func readRuntimeStatus(t *testing.T, path string) cameraRuntimeStatus {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var status cameraRuntimeStatus
	if err := json.Unmarshal(data, &status); err != nil {
		t.Fatal(err)
	}
	return status
}

func TestRuntimeStatusSPSAndHealth(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "camera.json")
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	w := newRuntimeStatusWriterWithClock(
		path, "hd", func() time.Time { return now },
	)
	w.setStatus("probing")
	w.observeSPS(640, 360, 66, 30)
	w.setStatus("online")

	now = now.Add(10 * time.Second)
	w.observeSPS(1920, 1080, 66, 41)
	w.updateHealth(countersSnapshot{
		idrFramesWithLoss:     2,
		vidDropped:            3,
		missingFragmentsTotal: 4,
		ackSeenPending:        5,
		extendedMediaRejected: 6,
	})

	status := readRuntimeStatus(t, path)
	if status.SchemaVersion != 2 || status.Status != "online" {
		t.Fatalf("unexpected status header: %+v", status)
	}
	if status.ProbeResolution == nil || status.ProbeResolution.Width != 640 ||
		status.ProbeResolution.Height != 360 {
		t.Fatalf("probe resolution=%+v", status.ProbeResolution)
	}
	if status.ActualResolution == nil || status.ActualResolution.Width != 1920 ||
		status.ActualResolution.Height != 1080 {
		t.Fatalf("actual resolution=%+v", status.ActualResolution)
	}
	if !status.HDTransition.Observed || status.HDTransition.Elapsed == nil ||
		*status.HDTransition.Elapsed != 10000 {
		t.Fatalf("HD transition=%+v", status.HDTransition)
	}
	if status.Health.GappedIDRs != 2 || status.Health.DroppedFrames != 3 ||
		status.Health.MissingFragments != 4 || status.Health.ACKPending != 5 ||
		status.Health.ExtendedMediaRejected != 6 {
		t.Fatalf("health=%+v", status.Health)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("status mode=%v, want 0600", info.Mode().Perm())
	}
	matches, err := filepath.Glob(filepath.Join(dir, ".petlibro-camera-status-*"))
	if err != nil || len(matches) != 0 {
		t.Fatalf("temporary status files=%v err=%v", matches, err)
	}
}

func TestRuntimeStatusPreservesErrorOnClose(t *testing.T) {
	path := filepath.Join(t.TempDir(), "camera.json")
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	w := newRuntimeStatusWriterWithClock(
		path, "sd", func() time.Time { return now },
	)
	w.setStatus("error")
	w.markOffline()

	status := readRuntimeStatus(t, path)
	if status.Status != "error" {
		t.Fatalf("status=%q, want error", status.Status)
	}
	if status.HDTransition.Observed || status.HDTransition.Elapsed != nil {
		t.Fatalf("unexpected HD transition=%+v", status.HDTransition)
	}
}
