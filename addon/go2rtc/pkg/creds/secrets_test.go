package creds

import (
	"bytes"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestString(t *testing.T) {
	AddSecret("admin")
	AddSecret("pa$$word")

	s := SecretString("rtsp://admin:pa$$word@192.168.1.123/stream1")
	require.Equal(t, "rtsp://***@192.168.1.123/stream1", s)
}

func TestSecretStringRedactsSensitiveStructuredFields(t *testing.T) {
	for _, input := range []string{
		`{"level":"debug","auth_key":"must-not-leak","message":"connected"}`,
		`{"level":"debug","auth":"must-not-leak","message":"connected"}`,
		`level=debug camera_auth_info=must-not-leak message=connected`,
		`level=debug authentication=must-not-leak message=connected`,
		`level=debug password="must not leak" message=connected`,
	} {
		rendered := SecretString(input)
		require.NotContains(t, rendered, "must-not-leak")
		require.NotContains(t, rendered, "must not leak")
		require.Contains(t, rendered, redactedSecret)
	}

	require.Contains(t, SecretString(`keyframe=true session_key_id=7`), "keyframe=true")
}

func TestRegisterEndpointSecretsPreservesUsefulShape(t *testing.T) {
	const endpoint = "petlibro://192.0.2.10/api/camera/PLAF20300000000ABCD0" +
		"?uid=PLAF20300000000ABCD0&quality=hd&audio=true&status_file=%2Fdata%2Fprivate.json"
	RegisterEndpointSecrets(endpoint)

	rendered := SecretString(endpoint)
	require.NotContains(t, rendered, "192.0.2.10")
	require.NotContains(t, rendered, "PLAF20300000000ABCD0")
	require.NotContains(t, rendered, "/data/private.json")
	require.Contains(t, rendered, "petlibro://***")
	require.Contains(t, rendered, "/api/camera/***")
	require.Contains(t, rendered, "uid=***")
	require.Contains(t, rendered, "quality=hd")
	require.Contains(t, rendered, "audio=true")
}

func TestSecretWriterSanitizesMalformedEndpointAndReportsCompleteWrite(t *testing.T) {
	const input = `source=petlibro://camera.invalid?uid=IDENTIFIER%zz&auth_key=AUTHSECRET quality=hd`
	var output bytes.Buffer

	n, err := SecretWriter(&output).Write([]byte(input))
	require.NoError(t, err)
	require.Equal(t, len(input), n)
	require.NotContains(t, output.String(), "IDENTIFIER")
	require.NotContains(t, output.String(), "AUTHSECRET")
	require.Contains(t, output.String(), "petlibro://camera.invalid?uid=***")
	require.Contains(t, output.String(), "quality=hd")
}
