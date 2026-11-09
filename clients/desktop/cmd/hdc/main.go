// Command hdc is the headless helper for the HaoDrive mapping rules.
//
// It does no networking: give it a JSON dump of channel messages and it prints
// the logical files the clients would show.  Useful for debugging a channel
// by hand and for cross-checking the Kotlin implementation against the same
// fixtures.
//
//	hdc rebuild messages.json
//	hdc rebuild --overlay map.txt messages.json
//	hdc overlay map.txt messages.json
//
// The input may be a bare JSON array of messages or an object with a
// "messages" field:
//
//	{"messages": [{"id": 101, "file_name": "a.bin.part001", "size": 100,
//	               "caption": "[TD-PART 1/2]"}]}
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"github.com/luojinghao6-art/haodrive/clients/desktop/internal/hdmap"
)

func usage() {
	fmt.Fprintln(os.Stderr, `usage:
  hdc rebuild [--overlay map.txt] messages.json
  hdc overlay map.txt messages.json

messages.json: array of messages, or {"messages": [...]}.
Each message: {"id": 101, "file_name": "a.bin.part001", "size": 100, "caption": "[TD-PART 1/2]"}`)
	os.Exit(2)
}

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	switch os.Args[1] {
	case "rebuild":
		rebuild(os.Args[2:])
	case "overlay":
		overlay(os.Args[2:])
	case "-h", "--help", "help":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n\n", os.Args[1])
		usage()
	}
}

func rebuild(args []string) {
	flags := flag.NewFlagSet("rebuild", flag.ExitOnError)
	overlayPath := flags.String("overlay", "", "optional map.txt to apply")
	flags.Parse(args)
	if flags.NArg() != 1 {
		usage()
	}
	messages, err := readMessages(flags.Arg(0))
	if err != nil {
		fail(err)
	}
	files := hdmap.Rebuild(messages)
	if *overlayPath != "" {
		raw, err := os.ReadFile(*overlayPath)
		if err != nil {
			fail(err)
		}
		files = hdmap.ParseOverlay(string(raw)).Apply(files)
	}
	printFiles(files)
}

func overlay(args []string) {
	if len(args) != 2 {
		usage()
	}
	raw, err := os.ReadFile(args[0])
	if err != nil {
		fail(err)
	}
	messages, err := readMessages(args[1])
	if err != nil {
		fail(err)
	}
	files := hdmap.ParseOverlay(string(raw)).Apply(hdmap.Rebuild(messages))
	printFiles(files)
}

func readMessages(path string) ([]hdmap.Message, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var list []hdmap.Message
	if err := json.Unmarshal(raw, &list); err == nil {
		return list, nil
	}
	var wrapped struct {
		Messages []hdmap.Message `json:"messages"`
	}
	if err := json.Unmarshal(raw, &wrapped); err != nil {
		return nil, fmt.Errorf("%s: expected a message array or {\"messages\": [...]}: %w", path, err)
	}
	return wrapped.Messages, nil
}

func printFiles(files []hdmap.File) {
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(files); err != nil {
		fail(err)
	}
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "hdc:", err)
	os.Exit(1)
}
