// Package console gives every service the same shape of log line.
//
// This is not decoration. When six processes interleave into one terminal, the
// difference between "I can see the system" and "I am reading noise" is column
// alignment and a stable colour per process. Observability starts here, long
// before you have a tracing backend.
package console

import (
	"fmt"
	"hash/fnv"
	"os"
	"strings"
	"sync"
	"time"
)

// One writer lock, because six goroutines writing to one fd will interleave
// mid-line otherwise. A torn log line is worse than no log line.
var mu sync.Mutex

var palette = []string{
	"\033[36m", "\033[32m", "\033[33m", "\033[35m", "\033[34m", "\033[31m",
}

const reset = "\033[0m"

var useColour = colourSupported()

func colourSupported() bool {
	if os.Getenv("NO_COLOR") != "" {
		return false
	}
	fi, err := os.Stdout.Stat()
	if err != nil {
		return false
	}
	return fi.Mode()&os.ModeCharDevice != 0
}

// Logger prints for one named process.
type Logger struct {
	name   string
	colour string
}

func New(name string) *Logger {
	h := fnv.New32a()
	_, _ = h.Write([]byte(name))
	return &Logger{name: name, colour: palette[int(h.Sum32())%len(palette)]}
}

// Say prints a line that is not about a specific record — startup, shutdown,
// a decision with no message attached.
func (l *Logger) Say(format string, a ...any) {
	l.emit(" ", "", fmt.Sprintf(format, a...))
}

// Produced prints an append to the log. The arrow points away from us.
func (l *Logger) Produced(topic string, partition int, offset int64, ev, note string) {
	l.emit("->", where(topic, partition, offset), pad(ev, 17)+note)
}

// Consumed prints a read from the log. The arrow points at us.
//
// Printing topic/partition@offset on every single line is the single highest
// value habit in this lab: it turns "Kafka is a queue, I guess" into something
// you can watch. You can see which partition a key landed on, you can see
// offsets advance, and in scenarios/04 you can watch the *same offset* get
// processed twice.
func (l *Logger) Consumed(topic string, partition int, offset int64, ev, note string) {
	l.emit("<-", where(topic, partition, offset), pad(ev, 17)+note)
}

// Warn prints a line that should catch your eye.
func (l *Logger) Warn(format string, a ...any) {
	l.emit(" !", "", fmt.Sprintf(format, a...))
}

func (l *Logger) emit(arrow, loc, text string) {
	name := pad(l.name, 12)
	if useColour {
		name = l.colour + name + reset
	}
	mu.Lock()
	defer mu.Unlock()
	fmt.Printf("%s %s %s %s %s\n",
		time.Now().Format("15:04:05.000"), name, arrow, pad(loc, 17), text)
}

func where(topic string, partition int, offset int64) string {
	return fmt.Sprintf("%s/%d@%04d", topic, partition, offset)
}

func pad(s string, n int) string {
	if len(s) >= n {
		return s + " "
	}
	return s + strings.Repeat(" ", n-len(s))
}
