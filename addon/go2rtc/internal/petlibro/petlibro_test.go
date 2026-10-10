package petlibro

import (
	"strings"
	"testing"

	"github.com/AlexxIT/go2rtc/pkg/creds"
)

func TestPetlibroSourceUsesCentralSanitizer(t *testing.T) {
	const uid = "PLAF20300000000ABCD0"
	for _, source := range []string{
		"petlibro://192.0.2.10?uid=" + uid + "&quality=hd&status_file=/data/private.json",
		"petlibro://?uid=" + uid + "&subnet=192.0.2.0%2F24&trace_ack=1",
	} {
		creds.RegisterEndpointSecrets(source)
		redacted := creds.SecretString(source)
		if strings.Contains(redacted, uid) {
			t.Fatalf("redacted URL still contains UID: %s", redacted)
		}
		if !strings.Contains(redacted, "uid=***") {
			t.Fatalf("redacted URL does not identify the hidden UID field: %s", redacted)
		}
		if strings.Contains(redacted, "192.0.2.") || strings.Contains(redacted, "/data/private.json") {
			t.Fatalf("redacted URL leaked endpoint identity: %s", redacted)
		}
		if !strings.Contains(redacted, "quality=hd") && !strings.Contains(redacted, "trace_ack=1") {
			t.Fatalf("redacted URL lost non-sensitive diagnostics: %s", redacted)
		}
	}
}
