package creds

import (
	"io"
	"net/http"
	"net/url"
	"path"
	"regexp"
	"slices"
	"strings"
	"sync"
)

const redactedSecret = "***"

var (
	// Field-name matching is a final safety net for values that were not
	// registered explicitly. Keep the markers specific so ordinary diagnostic
	// names such as "keyframe" aren't hidden.
	sensitiveJSONField      = regexp.MustCompile(`(?i)("[^"\\]*(?:password|passwd|secret|credential|token|authorization|camera[_-]?auth(?:info)?|auth[_-]?key|api[_-]?key|access[_-]?key|private[_-]?key|session[_-]?key)[^"\\]*"\s*:\s*)("(?:\\.|[^"\\])*"|[^,}\s]+)`)
	sensitiveTextField      = regexp.MustCompile(`(?i)(\b[^\s=]*(?:password|passwd|secret|credential|token|authorization|camera[_-]?auth(?:info)?|auth[_-]?key|api[_-]?key|access[_-]?key|private[_-]?key|session[_-]?key)[^\s=]*=)("(?:\\.|[^"\\])*"|[^\s]+)`)
	sensitiveExactJSONField = regexp.MustCompile(`(?i)("(?:auth|authentication)"\s*:\s*)("(?:\\.|[^"\\])*"|[^,}\s]+)`)
	sensitiveExactTextField = regexp.MustCompile(`(?i)(\b(?:auth|authentication)=)("(?:\\.|[^"\\])*"|[^\s]+)`)
	sensitiveURLQuery       = regexp.MustCompile(`(?i)([?&](?:password|passwd|secret|credential|token|authorization|camera[_-]?auth(?:info)?|auth[_-]?key|api[_-]?key|access[_-]?key|private[_-]?key|session[_-]?key|uid|uuid|serial|device[_-]?id|user[_-]?id|email|ssid|host|address|subnet|status[_-]?file|dump(?:[_-]?(?:d2c|c2d))?[_-]?plain)=)[^&#\s"']*`)
)

func AddSecret(value string) {
	if value == "" {
		return
	}

	secretsMu.Lock()
	defer secretsMu.Unlock()

	if slices.Contains(secrets, value) {
		return
	}

	secrets = append(secrets, value)
	secretsReplacer = nil
}

// RegisterEndpointSecrets records identifying URL values once so the
// process-wide SecretWriter removes them from every later log event. It keeps
// useful generic context such as the scheme, route prefix, query names, and
// non-identifying options.
//
// Endpoint identity is best-effort. Credentials and secret-shaped structured
// fields are additionally protected by SecretString regardless of whether
// this function could parse the URL.
func RegisterEndpointSecrets(rawURL string) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return
	}
	if u.User != nil {
		addURLSecret(u.User.Username())
		if password, ok := u.User.Password(); ok {
			addURLSecret(password)
		}
	}
	addURLSecret(u.Hostname())

	// Preserve generic route prefixes while hiding a likely endpoint identity
	// in the final path segment.
	if segment := path.Base(strings.TrimSuffix(u.EscapedPath(), "/")); segment != "." && segment != "/" && len(segment) >= 6 {
		if decoded, decodeErr := url.PathUnescape(segment); decodeErr == nil {
			addURLSecret(decoded)
		} else {
			addURLSecret(segment)
		}
	}

	for key, values := range u.Query() {
		if !sensitiveEndpointField(key) {
			continue
		}
		for _, value := range values {
			addURLSecret(value)
		}
	}
}

func addURLSecret(value string) {
	AddSecret(value)
	AddSecret(url.QueryEscape(value))
	AddSecret(url.PathEscape(value))
}

func sensitiveEndpointField(key string) bool {
	normalized := strings.NewReplacer("_", "", "-", "", ".", "").Replace(strings.ToLower(key))
	for _, marker := range []string{
		"password", "passwd", "secret", "credential", "token", "auth", "apikey",
		"accesskey", "privatekey", "sessionkey", "uid", "uuid", "serial", "deviceid",
		"username", "userid", "email", "ssid", "host", "address", "subnet",
		"statusfile", "dumpplain", "dumpd2cplain", "dumpc2dplain",
	} {
		if strings.Contains(normalized, marker) {
			return true
		}
	}
	return false
}

var secrets []string
var secretsMu sync.Mutex
var secretsReplacer *strings.Replacer
var userinfoRegexp *regexp.Regexp

func getReplacer() *strings.Replacer {
	secretsMu.Lock()
	defer secretsMu.Unlock()

	if secretsReplacer == nil {
		oldnew := make([]string, 0, 2*len(secrets))
		for _, s := range secrets {
			oldnew = append(oldnew, s, "***")
		}
		secretsReplacer = strings.NewReplacer(oldnew...)
	}

	if userinfoRegexp == nil {
		userinfoRegexp = regexp.MustCompile(`://[` + userinfo + `]+@`)
	}

	return secretsReplacer
}

// Uniform Resource Identifier (URI)
// https://datatracker.ietf.org/doc/html/rfc3986
const (
	unreserved = `A-Za-z0-9-._~`
	subdelims  = `!$&'()*+,;=`
	userinfo   = unreserved + subdelims + `%:`
)

func SecretString(s string) string {
	re := getReplacer()
	s = userinfoRegexp.ReplaceAllString(s, `://***@`)
	s = re.Replace(s)
	s = sensitiveURLQuery.ReplaceAllString(s, `${1}`+redactedSecret)
	s = sensitiveJSONField.ReplaceAllString(s, `${1}"`+redactedSecret+`"`)
	s = sensitiveTextField.ReplaceAllString(s, `${1}`+redactedSecret)
	s = sensitiveExactJSONField.ReplaceAllString(s, `${1}"`+redactedSecret+`"`)
	s = sensitiveExactTextField.ReplaceAllString(s, `${1}`+redactedSecret)
	return s
}

func SecretWrite(w io.Writer, s string) (n int, err error) {
	redacted := SecretString(s)
	written, err := io.WriteString(w, redacted)
	if err != nil {
		return written, err
	}
	if written != len(redacted) {
		return written, io.ErrShortWrite
	}
	// SecretWriter consumed the complete original record even when redaction
	// changed its encoded length.
	return len(s), nil
}

func SecretWriter(w io.Writer) io.Writer {
	return &secretWriter{w}
}

type secretWriter struct {
	w io.Writer
}

func (s *secretWriter) Write(b []byte) (int, error) {
	return SecretWrite(s.w, string(b))
}

func SecretResponse(w http.ResponseWriter) http.ResponseWriter {
	return &secretResponse{w}
}

type secretResponse struct {
	http.ResponseWriter
}

func (s *secretResponse) Write(b []byte) (int, error) {
	return SecretWrite(s.ResponseWriter, string(b))
}
