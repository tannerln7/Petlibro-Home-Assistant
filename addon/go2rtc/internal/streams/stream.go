package streams

import (
	"encoding/json"
	"sync"
	"sync/atomic"

	"github.com/AlexxIT/go2rtc/pkg/core"
)

type Stream struct {
	id        uint32
	producers []*Producer
	consumers []core.Consumer
	mu        sync.Mutex
	pending   atomic.Int32
}

func newStream() *Stream {
	return &Stream{id: core.NewID()}
}

func (s *Stream) ID() uint32 {
	return s.id
}

func (s *Stream) addSource(source string) {
	producer := NewProducer(source)
	producer.streamID = s.id
	s.producers = append(s.producers, producer)
}

func NewStream(source any) *Stream {
	switch source := source.(type) {
	case string:
		s := newStream()
		s.addSource(source)
		return s
	case []string:
		s := newStream()
		for _, str := range source {
			s.addSource(str)
		}
		return s
	case []any:
		s := newStream()
		for _, src := range source {
			str, ok := src.(string)
			if !ok {
				log.Error().Msgf("[stream] NewStream: Expected string, got %v", src)
				continue
			}
			s.addSource(str)
		}
		return s
	case map[string]any:
		return NewStream(source["url"])
	case nil:
		return newStream()
	default:
		panic(core.Caller())
	}
}

func (s *Stream) Sources() []string {
	sources := make([]string, 0, len(s.producers))
	for _, prod := range s.producers {
		sources = append(sources, prod.url)
	}
	return sources
}

func (s *Stream) SetSource(source string) {
	for _, prod := range s.producers {
		prod.SetSource(source)
	}
}

func (s *Stream) RemoveConsumer(cons core.Consumer) {
	_ = cons.Stop()

	s.mu.Lock()
	removed := false
	for i, consumer := range s.consumers {
		if consumer == cons {
			s.consumers = append(s.consumers[:i], s.consumers[i+1:]...)
			removed = true
			break
		}
	}
	remaining := len(s.consumers)
	s.mu.Unlock()
	if removed {
		log.Debug().Uint32("stream_id", s.id).Uint32("consumer_id", connectionID(cons)).
			Str("consumer_type", connectionType(cons)).Int("consumer_count", remaining).
			Msg("[streams] consumer detached")
	}

	s.stopProducers()
}

func (s *Stream) AddProducer(prod core.Producer) {
	producer := &Producer{id: core.NewID(), streamID: s.id, conn: prod, state: stateExternal, url: "external"}
	s.mu.Lock()
	s.producers = append(s.producers, producer)
	s.mu.Unlock()
}

func (s *Stream) RemoveProducer(prod core.Producer) {
	s.mu.Lock()
	for i, producer := range s.producers {
		if producer.conn == prod {
			s.producers = append(s.producers[:i], s.producers[i+1:]...)
			break
		}
	}
	s.mu.Unlock()
}

func (s *Stream) stopProducers() {
	if s.pending.Load() > 0 {
		log.Trace().Msg("[streams] skip stop pending producer")
		return
	}

	s.mu.Lock()
producers:
	for _, producer := range s.producers {
		for _, track := range producer.receivers {
			if len(track.Senders()) > 0 {
				continue producers
			}
		}
		for _, track := range producer.senders {
			if len(track.Senders()) > 0 {
				continue producers
			}
		}
		producer.stop()
	}
	s.mu.Unlock()
}

func (s *Stream) MarshalJSON() ([]byte, error) {
	var info = struct {
		Producers []*Producer     `json:"producers"`
		Consumers []core.Consumer `json:"consumers"`
	}{
		Producers: s.producers,
		Consumers: s.consumers,
	}
	return json.Marshal(info)
}
