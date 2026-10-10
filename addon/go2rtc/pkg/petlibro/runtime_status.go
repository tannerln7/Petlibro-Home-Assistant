package petlibro

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

const runtimeStatusSchemaVersion = 2

type runtimeResolution struct {
	Width      uint16 `json:"width"`
	Height     uint16 `json:"height"`
	ProfileIDC byte   `json:"profile_idc"`
	LevelIDC   byte   `json:"level_idc"`
	ObservedAt string `json:"observed_at"`
}

type runtimeHDTransition struct {
	Observed bool   `json:"observed"`
	Elapsed  *int64 `json:"elapsed_ms"`
}

type runtimeHealth struct {
	GappedIDRs            uint64 `json:"gapped_idrs"`
	DroppedFrames         uint64 `json:"dropped_frames"`
	MissingFragments      uint64 `json:"missing_fragments"`
	ACKPending            uint64 `json:"ack_pending"`
	ExtendedMediaRejected uint64 `json:"extended_media_rejected"`
}

type cameraRuntimeStatus struct {
	SchemaVersion    int                 `json:"schema_version"`
	Status           string              `json:"status"`
	RequestedQuality string              `json:"requested_quality"`
	ProbeResolution  *runtimeResolution  `json:"probe_resolution"`
	ActualResolution *runtimeResolution  `json:"actual_resolution"`
	HDTransition     runtimeHDTransition `json:"hd_transition"`
	LastUpdate       string              `json:"last_update"`
	Health           runtimeHealth       `json:"health"`
}

// runtimeStatusWriter is the structured hand-off between the PLAF203 camera
// adapter and the add-on controller. It serializes every mutation and replaces the
// destination atomically so readers never observe partial JSON.
type runtimeStatusWriter struct {
	mu         sync.Mutex
	path       string
	now        func() time.Time
	firstSPSAt time.Time
	lastUpdate time.Time
	state      cameraRuntimeStatus
	lastError  string
}

func newRuntimeStatusWriter(path, quality string) *runtimeStatusWriter {
	return newRuntimeStatusWriterWithClock(path, quality, time.Now)
}

func newRuntimeStatusWriterWithClock(
	path, quality string, now func() time.Time,
) *runtimeStatusWriter {
	if path == "" {
		return nil
	}
	w := &runtimeStatusWriter{
		path: path,
		now:  now,
		state: cameraRuntimeStatus{
			SchemaVersion:    runtimeStatusSchemaVersion,
			Status:           "starting",
			RequestedQuality: quality,
			HDTransition:     runtimeHDTransition{},
		},
	}
	w.write()
	return w
}

func (w *runtimeStatusWriter) setStatus(status string) {
	if w == nil {
		return
	}
	w.mu.Lock()
	w.state.Status = status
	w.writeLocked()
	w.mu.Unlock()
}

func (w *runtimeStatusWriter) markOffline() {
	if w == nil {
		return
	}
	w.mu.Lock()
	if w.state.Status != "error" {
		w.state.Status = "offline"
	}
	w.writeLocked()
	w.mu.Unlock()
}

func (w *runtimeStatusWriter) observeSPS(width, height uint16, profileIDC, levelIDC byte) {
	if w == nil {
		return
	}
	w.mu.Lock()
	now := w.now().UTC()
	resolution := &runtimeResolution{
		Width:      width,
		Height:     height,
		ProfileIDC: profileIDC,
		LevelIDC:   levelIDC,
		ObservedAt: now.Format(time.RFC3339Nano),
	}
	if w.state.ProbeResolution == nil {
		w.firstSPSAt = now
		probe := *resolution
		w.state.ProbeResolution = &probe
	}
	w.state.ActualResolution = resolution
	if w.state.RequestedQuality == "hd" && width >= 1920 && height >= 1080 &&
		w.state.ProbeResolution != nil &&
		(w.state.ProbeResolution.Width < width || w.state.ProbeResolution.Height < height) {
		elapsed := now.Sub(w.firstSPSAt).Milliseconds()
		if elapsed < 0 {
			elapsed = 0
		}
		w.state.HDTransition.Observed = true
		w.state.HDTransition.Elapsed = &elapsed
	}
	w.writeLocked()
	w.mu.Unlock()
}

func (w *runtimeStatusWriter) updateHealth(snapshot countersSnapshot) {
	if w == nil {
		return
	}
	w.mu.Lock()
	w.state.Health = runtimeHealth{
		GappedIDRs:            snapshot.idrFramesWithLoss,
		DroppedFrames:         snapshot.vidDropped,
		MissingFragments:      snapshot.missingFragmentsTotal,
		ACKPending:            snapshot.ackSeenPending,
		ExtendedMediaRejected: snapshot.extendedMediaRejected,
	}
	w.writeLocked()
	w.mu.Unlock()
}

func (w *runtimeStatusWriter) write() {
	if w == nil {
		return
	}
	w.mu.Lock()
	w.writeLocked()
	w.mu.Unlock()
}

func (w *runtimeStatusWriter) writeLocked() {
	now := w.now().UTC()
	if !w.lastUpdate.IsZero() && !now.After(w.lastUpdate) {
		now = w.lastUpdate.Add(time.Nanosecond)
	}
	w.lastUpdate = now
	w.state.LastUpdate = now.Format(time.RFC3339Nano)
	data, err := json.Marshal(w.state)
	if err == nil {
		err = atomicWriteRuntimeStatus(w.path, append(data, '\n'))
	}
	if err == nil {
		w.lastError = ""
		return
	}
	message := err.Error()
	if message != w.lastError {
		log.Warn().Err(err).Msg("petlibro: write camera runtime status")
		w.lastError = message
	}
}

func atomicWriteRuntimeStatus(path string, data []byte) (err error) {
	dir := filepath.Dir(path)
	if err = os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("create status directory: %w", err)
	}
	temporary, err := os.CreateTemp(dir, ".petlibro-camera-status-*")
	if err != nil {
		return fmt.Errorf("create status temporary file: %w", err)
	}
	temporaryPath := temporary.Name()
	defer func() {
		_ = temporary.Close()
		if err != nil {
			_ = os.Remove(temporaryPath)
		}
	}()
	if err = temporary.Chmod(0o600); err != nil {
		return fmt.Errorf("set status file mode: %w", err)
	}
	if _, err = temporary.Write(data); err != nil {
		return fmt.Errorf("write status file: %w", err)
	}
	if err = temporary.Sync(); err != nil {
		return fmt.Errorf("sync status file: %w", err)
	}
	if err = temporary.Close(); err != nil {
		return fmt.Errorf("close status file: %w", err)
	}
	if err = os.Rename(temporaryPath, path); err != nil {
		return fmt.Errorf("replace status file: %w", err)
	}
	return nil
}
