package streams

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/core"
)

type state byte

const (
	stateNone state = iota
	stateMedias
	stateTracks
	stateStart
	stateExternal
	stateInternal
)

type Producer struct {
	core.Listener
	id         uint32
	streamID   uint32
	generation uint32

	url      string
	template string

	conn      core.Producer
	receivers []*core.Receiver
	senders   []*core.Receiver

	state    state
	mu       sync.Mutex
	workerID int
}

const SourceTemplate = "{input}"

func NewProducer(source string) *Producer {
	if strings.Contains(source, SourceTemplate) {
		return &Producer{id: core.NewID(), template: source}
	}

	return &Producer{id: core.NewID(), url: source}
}

type connectionIdentifier interface {
	GetID() uint32
}

func connectionID(conn any) uint32 {
	if identified, ok := conn.(connectionIdentifier); ok {
		return identified.GetID()
	}
	return 0
}

func connectionType(conn any) string {
	return fmt.Sprintf("%T", conn)
}

func sourceScheme(source string) string {
	if i := strings.IndexByte(source, ':'); i > 0 {
		return source[:i]
	}
	return "unknown"
}

func (p *Producer) SetSource(s string) {
	if p.template == "" {
		p.url = s
	} else {
		p.url = strings.Replace(p.template, SourceTemplate, s, 1)
	}
}

func (p *Producer) Dial() error {
	p.mu.Lock()
	defer p.mu.Unlock()

	if p.state == stateNone {
		nextGeneration := p.generation + 1
		log.Debug().Uint32("stream_id", p.streamID).Uint32("stream_producer_id", p.id).
			Uint32("producer_generation", nextGeneration).Str("source_scheme", sourceScheme(p.url)).
			Msg("[streams] producer dial started")
		conn, err := GetProducer(p.url)
		if err != nil {
			log.Debug().Uint32("stream_id", p.streamID).Uint32("stream_producer_id", p.id).
				Uint32("producer_generation", nextGeneration).Str("source_scheme", sourceScheme(p.url)).
				Err(err).Msg("[streams] producer dial failed")
			return err
		}

		p.conn = conn
		p.generation = nextGeneration
		p.state = stateMedias
		log.Debug().Uint32("stream_id", p.streamID).Uint32("stream_producer_id", p.id).
			Uint32("producer_generation", p.generation).Uint32("producer_id", connectionID(conn)).
			Str("source_scheme", sourceScheme(p.url)).Msg("[streams] producer dial completed")
	}

	return nil
}

func (p *Producer) GetMedias() []*core.Media {
	p.mu.Lock()
	defer p.mu.Unlock()

	if p.conn == nil {
		return nil
	}

	return p.conn.GetMedias()
}

func (p *Producer) GetTrack(media *core.Media, codec *core.Codec) (*core.Receiver, error) {
	p.mu.Lock()
	defer p.mu.Unlock()

	if p.state == stateNone {
		return nil, errors.New("get track from none state")
	}

	for _, track := range p.receivers {
		if track.Codec == codec {
			return track, nil
		}
	}

	track, err := p.conn.GetTrack(media, codec)
	if err != nil {
		return nil, err
	}

	p.receivers = append(p.receivers, track)

	if p.state == stateMedias {
		p.state = stateTracks
	}

	return track, nil
}

func (p *Producer) AddTrack(media *core.Media, codec *core.Codec, track *core.Receiver) error {
	p.mu.Lock()
	defer p.mu.Unlock()

	if p.state == stateNone {
		return errors.New("add track from none state")
	}

	if err := p.conn.(core.Consumer).AddTrack(media, codec, track); err != nil {
		return err
	}

	p.senders = append(p.senders, track)

	if p.state == stateMedias {
		p.state = stateTracks
	}

	return nil
}

func (p *Producer) MarshalJSON() ([]byte, error) {
	if conn := p.conn; conn != nil {
		return json.Marshal(conn)
	}
	info := map[string]string{"url": p.url}
	return json.Marshal(info)
}

// internals

func (p *Producer) start() {
	p.mu.Lock()
	defer p.mu.Unlock()

	if p.state != stateTracks {
		return
	}

	log.Debug().Uint32("stream_id", p.streamID).Uint32("stream_producer_id", p.id).
		Uint32("producer_generation", p.generation).Uint32("producer_id", connectionID(p.conn)).
		Msg("[streams] producer forwarding started")

	p.state = stateStart
	p.workerID++

	go p.worker(p.conn, p.workerID)
}

func (p *Producer) worker(conn core.Producer, workerID int) {
	if err := conn.Start(); err != nil {
		p.mu.Lock()
		closed := p.workerID != workerID
		p.mu.Unlock()

		if closed {
			return
		}

		log.Debug().Uint32("stream_id", p.streamID).Uint32("stream_producer_id", p.id).
			Uint32("producer_generation", p.generation).Uint32("producer_id", connectionID(conn)).
			Err(err).Msg("[streams] producer forwarding ended; reconnecting")
	}

	p.reconnect(workerID, 0)
}

func (p *Producer) reconnect(workerID, retry int) {
	p.mu.Lock()
	defer p.mu.Unlock()

	if p.workerID != workerID {
		log.Trace().Uint32("stream_id", p.streamID).Uint32("stream_producer_id", p.id).
			Msg("[streams] reconnect cancelled")
		return
	}

	nextGeneration := p.generation + 1
	oldProducerID := connectionID(p.conn)
	log.Debug().Uint32("stream_id", p.streamID).Uint32("stream_producer_id", p.id).
		Uint32("old_producer_id", oldProducerID).Uint32("producer_generation", nextGeneration).
		Int("retry", retry).Str("source_scheme", sourceScheme(p.url)).
		Msg("[streams] replacement producer dial started")

	conn, err := GetProducer(p.url)
	if err != nil {
		log.Debug().Uint32("stream_id", p.streamID).Uint32("stream_producer_id", p.id).
			Uint32("old_producer_id", oldProducerID).Uint32("producer_generation", nextGeneration).
			Int("retry", retry).Err(err).Msg("[streams] replacement producer dial failed")

		timeout := time.Minute
		if retry < 5 {
			timeout = time.Second
		} else if retry < 10 {
			timeout = time.Second * 5
		} else if retry < 20 {
			timeout = time.Second * 10
		}

		time.AfterFunc(timeout, func() {
			p.reconnect(workerID, retry+1)
		})
		return
	}
	newProducerID := connectionID(conn)
	log.Debug().Uint32("stream_id", p.streamID).Uint32("stream_producer_id", p.id).
		Uint32("old_producer_id", oldProducerID).Uint32("new_producer_id", newProducerID).
		Uint32("producer_generation", nextGeneration).
		Msg("[streams] replacement producer ready; transferring tracks before stopping previous producer")

	for _, media := range conn.GetMedias() {
		switch media.Direction {
		case core.DirectionRecvonly:
			for i, receiver := range p.receivers {
				codec := media.MatchCodec(receiver.Codec)
				if codec == nil {
					continue
				}

				track, err := conn.GetTrack(media, codec)
				if err != nil {
					continue
				}

				receiver.Replace(track)
				p.receivers[i] = track
				break
			}

		case core.DirectionSendonly:
			for _, sender := range p.senders {
				codec := media.MatchCodec(sender.Codec)
				if codec == nil {
					continue
				}

				_ = conn.(core.Consumer).AddTrack(media, codec, sender)
			}
		}
	}

	// stop previous connection after moving tracks (fix ghost exec/ffmpeg)
	_ = p.conn.Stop()
	// swap connections
	p.conn = conn
	p.generation = nextGeneration
	log.Debug().Uint32("stream_id", p.streamID).Uint32("stream_producer_id", p.id).
		Uint32("old_producer_id", oldProducerID).Uint32("producer_id", newProducerID).
		Uint32("producer_generation", p.generation).
		Msg("[streams] producer replacement completed")

	go p.worker(conn, workerID)
}

func (p *Producer) stop() {
	p.mu.Lock()
	defer p.mu.Unlock()

	switch p.state {
	case stateExternal:
		log.Trace().Msgf("[streams] skip stop external producer")
		return
	case stateNone:
		log.Trace().Msgf("[streams] skip stop none producer")
		return
	case stateStart:
		p.workerID++
	}

	log.Debug().Uint32("stream_id", p.streamID).Uint32("stream_producer_id", p.id).
		Uint32("producer_generation", p.generation).Uint32("producer_id", connectionID(p.conn)).
		Msg("[streams] producer stopping")

	if p.conn != nil {
		_ = p.conn.Stop()
		p.conn = nil
	}

	p.state = stateNone
	p.receivers = nil
	p.senders = nil
}
