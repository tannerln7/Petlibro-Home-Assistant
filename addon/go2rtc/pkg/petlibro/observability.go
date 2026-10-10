package petlibro

import (
	"fmt"
	"sync/atomic"
)

// Camera, producer, and physical-session identifiers are process-local and
// deliberately unrelated to a camera UID or network endpoint. They are safe
// correlation values for logs and have no protocol meaning.
var (
	adapterSequence atomic.Uint64
	sessionSequence atomic.Uint64
)

type cameraCorrelation struct {
	adapterID      string
	producerID     uint32
	startupAttempt int
}

func newAdapterID() string {
	return fmt.Sprintf("ca-%06x", adapterSequence.Add(1))
}

func newSessionID() string {
	return fmt.Sprintf("ps-%06x", sessionSequence.Add(1))
}
