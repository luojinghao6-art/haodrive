// Package hdmap implements the mapping rules shared by the HaoDrive server and
// its standalone clients.
//
// One logical file is either a single Telegram message or a group of messages
// named "<name>.partNNN" whose captions read "[TD-PART i/N]".  Nothing here
// talks to Telegram: give it messages and it tells you what the files are.
// The exact rules are written down in docs/CLIENTS.md sections 7 and 8, and
// the cross-language test vectors live in clients/spec/vectors.json.
package hdmap

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	StateComplete   = "complete"
	StateIncomplete = "incomplete"
)

// captionRe matches exactly what the server writes for a part message.
var captionRe = regexp.MustCompile(`^\[TD-PART (\d+)/(\d+)\]$`)

// partSuffixRe strips the ".part001" tail when rebuilding the base name.
var partSuffixRe = regexp.MustCompile(`\.part\d+$`)

// Message is the subset of a Telegram message the mapping needs.
type Message struct {
	ID       int64  `json:"id"`
	FileName string `json:"file_name"`
	Size     int64  `json:"size"`
	Caption  string `json:"caption"`
}

// File is one logical file reconstructed from messages.
type File struct {
	Name       string  `json:"name"`
	Size       int64   `json:"size"`
	Parts      int     `json:"parts"`
	GroupKey   int64   `json:"group_key"`
	MessageIDs []int64 `json:"message_ids"`
	State      string  `json:"state"`
	// Set by an overlay; zero values mean "not set".
	Order   int  `json:"order,omitempty"`
	Renamed bool `json:"renamed,omitempty"`
}

// ParseCaption returns the part index and total, if the caption is a part
// caption.  It deliberately rejects anything the server could not have written.
func ParseCaption(caption string) (index int, total int, ok bool) {
	match := captionRe.FindStringSubmatch(strings.TrimSpace(caption))
	if match == nil {
		return 0, 0, false
	}
	index, errIndex := strconv.Atoi(match[1])
	total, errTotal := strconv.Atoi(match[2])
	if errIndex != nil || errTotal != nil || total < 1 || index < 1 || index > total {
		return 0, 0, false
	}
	return index, total, true
}

// FormatCaption is the inverse of ParseCaption.
func FormatCaption(index, total int) string {
	return fmt.Sprintf("[TD-PART %d/%d]", index, total)
}

// PartName returns the document name for one part.  A single-part file keeps
// its original name; the server never rewrites it.
func PartName(base string, index, total int) string {
	if total <= 1 {
		return base
	}
	return fmt.Sprintf("%s.part%03d", base, index)
}

// BaseName strips a ".partNNN" tail.  The caption decides whether a message is
// a part; this only recovers the original name.
func BaseName(name string) string {
	return partSuffixRe.ReplaceAllString(name, "")
}

// PlanParts splits size into Telegram parts, each at most partBytes (the last
// one may be shorter).  Same arithmetic as the server's parts.part_sizes.
func PlanParts(size, partBytes int64) []int64 {
	if size <= 0 {
		return []int64{0}
	}
	if partBytes <= 0 {
		partBytes = 1
	}
	count := (size + partBytes - 1) / partBytes
	out := make([]int64, 0, count)
	for offset := int64(0); offset < size; offset += partBytes {
		remaining := size - offset
		if remaining > partBytes {
			remaining = partBytes
		}
		out = append(out, remaining)
	}
	return out
}

type partEntry struct {
	index int
	msg   Message
}

type partGroup struct {
	base    string
	total   int
	entries []partEntry
}

func (g *partGroup) hasIndex(index int) bool {
	for _, entry := range g.entries {
		if entry.index == index {
			return true
		}
	}
	return false
}

func (g *partGroup) file() File {
	sort.SliceStable(g.entries, func(i, j int) bool {
		return g.entries[i].index < g.entries[j].index
	})
	out := File{Name: g.base, Parts: g.total, MessageIDs: make([]int64, 0, len(g.entries))}
	present := make(map[int]bool, len(g.entries))
	for _, entry := range g.entries {
		out.Size += entry.msg.Size
		out.MessageIDs = append(out.MessageIDs, entry.msg.ID)
		present[entry.index] = true
	}
	// The first part is the group key; without it fall back to the oldest
	// message so the key stays stable across rebuilds.
	out.GroupKey = g.entries[0].msg.ID
	for _, entry := range g.entries {
		if entry.index == 1 {
			out.GroupKey = entry.msg.ID
			break
		}
	}
	complete := len(g.entries) == g.total
	for index := 1; index <= g.total && complete; index++ {
		if !present[index] {
			complete = false
		}
	}
	if complete {
		out.State = StateComplete
	} else {
		out.State = StateIncomplete
	}
	return out
}

// Rebuild groups messages into logical files.  Duplicates are impossible for
// message ids, so the input order does not matter; output is ordered by the
// group key, which for a channel matches upload order.
func Rebuild(messages []Message) []File {
	sorted := make([]Message, len(messages))
	copy(sorted, messages)
	sort.SliceStable(sorted, func(i, j int) bool { return sorted[i].ID < sorted[j].ID })

	files := make([]File, 0, len(sorted))
	current := make(map[string]*partGroup)
	groups := make([]*partGroup, 0)

	for _, message := range sorted {
		index, total, ok := ParseCaption(message.Caption)
		if !ok {
			files = append(files, File{
				Name:       message.FileName,
				Size:       message.Size,
				Parts:      1,
				GroupKey:   message.ID,
				MessageIDs: []int64{message.ID},
				State:      StateComplete,
			})
			continue
		}
		base := BaseName(message.FileName)
		key := base + "\x00" + strconv.Itoa(total)
		group := current[key]
		// Two uploads of the same file name (same part count) look identical
		// in captions.  A repeated part index means a new run started, so the
		// earlier group is closed and this message belongs to a fresh one.
		if group == nil || group.hasIndex(index) {
			group = &partGroup{base: base, total: total}
			current[key] = group
			groups = append(groups, group)
		}
		group.entries = append(group.entries, partEntry{index: index, msg: message})
	}

	for _, group := range groups {
		files = append(files, group.file())
	}
	SortFiles(files)
	return files
}

// SortFiles orders files the way a client should list them: by group key
// (message id), which is upload order inside a channel.
func SortFiles(files []File) {
	sort.SliceStable(files, func(i, j int) bool {
		return files[i].GroupKey < files[j].GroupKey
	})
}

// ---- optional overlay (map.txt) -----------------------------------------

// Overlay carries the two things captions cannot: renames and manual order.
// It is optional; without it Rebuild alone is the complete answer.
type Overlay struct {
	Rev     int
	Updated time.Time
	Hash    string
	Channel int64
	Renames map[int64]string
	Orders  map[int64]int
}

// NewOverlay returns an empty overlay at revision 0.
func NewOverlay() *Overlay {
	return &Overlay{Rev: 0, Renames: map[int64]string{}, Orders: map[int64]int{}}
}

// ParseOverlay never fails: unknown or malformed lines are ignored so a
// hand-edited map.txt can never brick a client.  Empty input yields an empty
// overlay (revision 0).
func ParseOverlay(text string) *Overlay {
	overlay := NewOverlay()
	sum := sha256.Sum256([]byte(text))
	overlay.Hash = hex.EncodeToString(sum[:])

	for _, raw := range strings.Split(text, "\n") {
		line := strings.TrimSpace(strings.TrimSuffix(raw, "\r"))
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		switch {
		case strings.HasPrefix(line, "rev:"):
			if value, err := strconv.Atoi(strings.TrimSpace(strings.TrimPrefix(line, "rev:"))); err == nil && value >= 0 {
				overlay.Rev = value
			}
		case strings.HasPrefix(line, "updated:"):
			value := strings.TrimSpace(strings.TrimPrefix(line, "updated:"))
			if stamp, err := time.Parse(time.RFC3339, value); err == nil {
				overlay.Updated = stamp
			}
		case strings.HasPrefix(line, "rename "):
			if channel, key, name, ok := parseOverlayRename(strings.TrimPrefix(line, "rename ")); ok {
				overlay.Renames[key] = name
				if overlay.Channel == 0 {
					overlay.Channel = channel
				}
			}
		case strings.HasPrefix(line, "order "):
			if channel, key, value, ok := parseOverlayOrder(strings.TrimPrefix(line, "order ")); ok {
				overlay.Orders[key] = value
				if overlay.Channel == 0 {
					overlay.Channel = channel
				}
			}
		}
	}
	return overlay
}

// parseOverlayRename accepts both "rename <channel> <key> <name>" (the format
// in the spec) and a shorter "rename <key> <name>".
func parseOverlayRename(rest string) (channel, key int64, name string, ok bool) {
	fields := strings.Fields(rest)
	if len(fields) < 2 {
		return 0, 0, "", false
	}
	if len(fields) >= 3 {
		if channel, err := strconv.ParseInt(fields[0], 10, 64); err == nil {
			if key, err := strconv.ParseInt(fields[1], 10, 64); err == nil {
				parsed := strings.TrimSpace(strings.Join(fields[2:], " "))
				if parsed != "" {
					return channel, key, parsed, true
				}
				return 0, 0, "", false
			}
		}
	}
	key, err := strconv.ParseInt(fields[0], 10, 64)
	if err != nil {
		return 0, 0, "", false
	}
	parsed := strings.TrimSpace(strings.Join(fields[1:], " "))
	if parsed == "" {
		return 0, 0, "", false
	}
	return 0, key, parsed, true
}

func parseOverlayOrder(rest string) (channel, key int64, value int, ok bool) {
	fields := strings.Fields(rest)
	if len(fields) >= 3 {
		if channel, err := strconv.ParseInt(fields[0], 10, 64); err == nil {
			if key, err := strconv.ParseInt(fields[1], 10, 64); err == nil {
				if value, err := strconv.Atoi(fields[2]); err == nil {
					return channel, key, value, true
				}
			}
		}
	}
	if len(fields) != 2 {
		return 0, 0, 0, false
	}
	key, err := strconv.ParseInt(fields[0], 10, 64)
	if err != nil {
		return 0, 0, 0, false
	}
	value, err = strconv.Atoi(fields[1])
	if err != nil {
		return 0, 0, 0, false
	}
	return 0, key, value, true
}

// Apply renames and reorders files.  Files without an explicit order keep
// their group-key order and come after the ordered ones.
func (o *Overlay) Apply(files []File) []File {
	out := make([]File, len(files))
	for i, file := range files {
		out[i] = file
		out[i].MessageIDs = append([]int64(nil), file.MessageIDs...)
		if name, ok := o.Renames[file.GroupKey]; ok && name != "" {
			out[i].Name = name
			out[i].Renamed = true
		}
		if order, ok := o.Orders[file.GroupKey]; ok {
			out[i].Order = order
		}
	}
	sort.SliceStable(out, func(i, j int) bool {
		left, right := out[i].Order, out[j].Order
		if left > 0 && right > 0 {
			if left != right {
				return left < right
			}
		} else if left > 0 {
			return true
		} else if right > 0 {
			return false
		}
		return out[i].GroupKey < out[j].GroupKey
	})
	return out
}

// Merge picks the winner between two overlays: higher revision, then later
// update time, then the larger content hash (deterministic and symmetric).
func Merge(a, b *Overlay) *Overlay {
	if a == nil {
		return b.Clone()
	}
	if b == nil {
		return a.Clone()
	}
	if a.Rev != b.Rev {
		if a.Rev > b.Rev {
			return a.Clone()
		}
		return b.Clone()
	}
	if !a.Updated.Equal(b.Updated) {
		if a.Updated.After(b.Updated) {
			return a.Clone()
		}
		return b.Clone()
	}
	if a.Hash >= b.Hash {
		return a.Clone()
	}
	return b.Clone()
}

// Clone deep-copies an overlay (nil-safe).
func (o *Overlay) Clone() *Overlay {
	if o == nil {
		return nil
	}
	copyOverlay := &Overlay{
		Rev:     o.Rev,
		Updated: o.Updated,
		Hash:    o.Hash,
		Channel: o.Channel,
		Renames: make(map[int64]string, len(o.Renames)),
		Orders:  make(map[int64]int, len(o.Orders)),
	}
	for key, value := range o.Renames {
		copyOverlay.Renames[key] = value
	}
	for key, value := range o.Orders {
		copyOverlay.Orders[key] = value
	}
	return copyOverlay
}

// String serializes the overlay in the map.txt format, with keys sorted so the
// output is stable and diff-friendly.
func (o *Overlay) String() string {
	var builder strings.Builder
	fmt.Fprintf(&builder, "rev: %d\n", o.Rev)
	if !o.Updated.IsZero() {
		fmt.Fprintf(&builder, "updated: %s\n", o.Updated.UTC().Format(time.RFC3339))
	}
	keys := make([]int64, 0, len(o.Renames))
	for key := range o.Renames {
		keys = append(keys, key)
	}
	sort.Slice(keys, func(i, j int) bool { return keys[i] < keys[j] })
	for _, key := range keys {
		fmt.Fprintf(&builder, "rename %d %d %s\n", o.Channel, key, o.Renames[key])
	}
	orderKeys := make([]int64, 0, len(o.Orders))
	for key := range o.Orders {
		orderKeys = append(orderKeys, key)
	}
	sort.Slice(orderKeys, func(i, j int) bool { return orderKeys[i] < orderKeys[j] })
	for _, key := range orderKeys {
		fmt.Fprintf(&builder, "order %d %d %d\n", o.Channel, key, o.Orders[key])
	}
	return builder.String()
}
