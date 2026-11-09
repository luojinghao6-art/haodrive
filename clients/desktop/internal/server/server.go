// Package server exposes the local HTTP API and the embedded UI.
package server

import (
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"mime"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/luojinghao6-art/haodrive/clients/desktop/internal/hdmap"
	"github.com/luojinghao6-art/haodrive/clients/desktop/internal/tgclient"
)

//go:embed web
var webFS embed.FS

// Server wires the Telegram client to a local web UI.
type Server struct {
	tg      *tgclient.Client
	dataDir string
	config  config
	mu      sync.Mutex
	prog    progress
}

type config struct {
	APIID   int    `json:"api_id"`
	APIHash string `json:"api_hash"`
	Phone   string `json:"phone"`
}

type progress struct {
	Active bool   `json:"active"`
	Name   string `json:"name"`
	Sent   int64  `json:"sent"`
	Total  int64  `json:"total"`
	Error  string `json:"error,omitempty"`
}

// New builds a server bound to a data directory.
func New(tg *tgclient.Client, dataDir string) *Server {
	server := &Server{tg: tg, dataDir: dataDir}
	server.loadConfig()
	return server
}

func (s *Server) configPath() string { return filepath.Join(s.dataDir, "config.json") }

func (s *Server) loadConfig() {
	raw, err := os.ReadFile(s.configPath())
	if err != nil {
		return
	}
	_ = json.Unmarshal(raw, &s.config)
}

func (s *Server) saveConfig() error {
	raw, err := json.MarshalIndent(s.config, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(s.dataDir, 0o700); err != nil {
		return err
	}
	return os.WriteFile(s.configPath(), raw, 0o600)
}

// Handler returns the root mux.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/", s.index)
	mux.HandleFunc("/api/config", s.handleConfig)
	mux.HandleFunc("/api/state", s.handleState)
	mux.HandleFunc("/api/login", s.handleLogin)
	mux.HandleFunc("/api/code", s.handleCode)
	mux.HandleFunc("/api/password", s.handlePassword)
	mux.HandleFunc("/api/logout", s.handleLogout)
	mux.HandleFunc("/api/channels", s.handleChannels)
	mux.HandleFunc("/api/progress", s.handleProgress)
	mux.HandleFunc("/api/channels/", s.handleChannelScoped)
	return mux
}

func (s *Server) index(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	raw, err := webFS.ReadFile("web/index.html")
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	_, _ = w.Write(raw)
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func fail(w http.ResponseWriter, status int, err error) {
	writeJSON(w, status, map[string]string{"error": err.Error()})
}

func decode(r *http.Request, value any) error {
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		return err
	}
	return json.Unmarshal(body, value)
}

func (s *Server) handleConfig(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	s.mu.Lock()
	config := s.config
	s.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{
		"api_id":   config.APIID,
		"api_hash": config.APIHash,
		"phone":    config.Phone,
	})
}

func (s *Server) handleState(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.tg.State())
}

func (s *Server) handleLogin(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var payload struct {
		APIID   int    `json:"api_id"`
		APIHash string `json:"api_hash"`
		Phone   string `json:"phone"`
	}
	if err := decode(r, &payload); err != nil {
		fail(w, http.StatusBadRequest, err)
		return
	}
	if payload.APIID == 0 || payload.APIHash == "" || payload.Phone == "" {
		fail(w, http.StatusBadRequest, errors.New("api_id / api_hash / 手机号都要填"))
		return
	}
	s.mu.Lock()
	s.config = config{APIID: payload.APIID, APIHash: payload.APIHash, Phone: payload.Phone}
	s.mu.Unlock()
	if err := s.saveConfig(); err != nil {
		fail(w, http.StatusInternalServerError, err)
		return
	}
	if err := s.tg.Start(r.Context(), payload.APIID, payload.APIHash, payload.Phone); err != nil {
		fail(w, http.StatusConflict, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *Server) handleCode(w http.ResponseWriter, r *http.Request) {
	var payload struct {
		Code string `json:"code"`
	}
	if err := decode(r, &payload); err != nil {
		fail(w, http.StatusBadRequest, err)
		return
	}
	if err := s.tg.SubmitCode(strings.TrimSpace(payload.Code)); err != nil {
		fail(w, http.StatusConflict, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *Server) handlePassword(w http.ResponseWriter, r *http.Request) {
	var payload struct {
		Password string `json:"password"`
	}
	if err := decode(r, &payload); err != nil {
		fail(w, http.StatusBadRequest, err)
		return
	}
	if err := s.tg.SubmitPassword(payload.Password); err != nil {
		fail(w, http.StatusConflict, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *Server) handleLogout(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	_ = s.tg.Logout()
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *Server) handleChannels(w http.ResponseWriter, r *http.Request) {
	channels, err := s.tg.Channels(r.Context())
	if err != nil {
		fail(w, http.StatusConflict, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"channels": channels})
}

func (s *Server) handleProgress(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	value := s.prog
	s.mu.Unlock()
	writeJSON(w, http.StatusOK, value)
}

// handleChannelScoped routes /api/channels/{id}/files and /upload.
func (s *Server) handleChannelScoped(w http.ResponseWriter, r *http.Request) {
	rest := strings.TrimPrefix(r.URL.Path, "/api/channels/")
	parts := strings.Split(rest, "/")
	if len(parts) < 2 {
		http.NotFound(w, r)
		return
	}
	channelID, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil {
		fail(w, http.StatusBadRequest, errors.New("频道 id 不合法"))
		return
	}
	switch {
	case len(parts) == 3 && parts[1] == "files" && r.Method == http.MethodGet:
		s.handleFiles(w, r, channelID)
	case len(parts) == 3 && parts[1] == "upload" && r.Method == http.MethodPost:
		s.handleUpload(w, r, channelID)
	case len(parts) == 5 && parts[1] == "files" && parts[3] == "download":
		s.handleDownload(w, r, channelID, parts[2])
	default:
		http.NotFound(w, r)
	}
}

func (s *Server) handleFiles(w http.ResponseWriter, r *http.Request, channelID int64) {
	files, _, err := s.tg.Files(r.Context(), channelID, 500)
	if err != nil {
		fail(w, http.StatusConflict, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"files": files})
}

func (s *Server) handleUpload(w http.ResponseWriter, r *http.Request, channelID int64) {
	reader, err := r.MultipartReader()
	if err != nil {
		fail(w, http.StatusBadRequest, err)
		return
	}
	if err := os.MkdirAll(filepath.Join(s.dataDir, "tmp"), 0o700); err != nil {
		fail(w, http.StatusInternalServerError, err)
		return
	}
	var tempPath, name string
	var size int64
	for {
		part, err := reader.NextPart()
		if err == io.EOF {
			break
		}
		if err != nil {
			fail(w, http.StatusBadRequest, err)
			return
		}
		if part.FormName() != "file" {
			_ = part.Close()
			continue
		}
		name = filepath.Base(part.FileName())
		if name == "" || name == "." {
			name = "upload.bin"
		}
		handle, err := os.CreateTemp(filepath.Join(s.dataDir, "tmp"), "upload-*")
		if err != nil {
			fail(w, http.StatusInternalServerError, err)
			return
		}
		tempPath = handle.Name()
		size, err = io.Copy(handle, part)
		_ = handle.Close()
		_ = part.Close()
		if err != nil {
			_ = os.Remove(tempPath)
			fail(w, http.StatusBadRequest, err)
			return
		}
		break
	}
	if tempPath == "" {
		fail(w, http.StatusBadRequest, errors.New("没有收到文件"))
		return
	}
	defer os.Remove(tempPath)

	s.mu.Lock()
	s.prog = progress{Active: true, Name: name, Total: size}
	s.mu.Unlock()
	log.Printf("upload: %s (%d bytes) -> channel %d", name, size, channelID)

	ids, err := s.tg.UploadFile(r.Context(), channelID, tempPath, name, size,
		func(sent, total int64) {
			s.mu.Lock()
			s.prog.Sent = sent
			s.prog.Total = total
			s.mu.Unlock()
		})
	s.mu.Lock()
	s.prog.Active = false
	if err != nil {
		s.prog.Error = err.Error()
	}
	s.mu.Unlock()
	if err != nil {
		fail(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "name": name, "ids": ids})
}

// handleDownload streams one logical file, honouring Range for preview.
func (s *Server) handleDownload(w http.ResponseWriter, r *http.Request, channelID int64, keyText string) {
	key, err := strconv.ParseInt(keyText, 10, 64)
	if err != nil {
		fail(w, http.StatusBadRequest, errors.New("文件 id 不合法"))
		return
	}
	files, docs, err := s.tg.Files(r.Context(), channelID, 500)
	if err != nil {
		fail(w, http.StatusConflict, err)
		return
	}
	var target *hdmap.File
	for index := range files {
		if files[index].GroupKey == key {
			target = &files[index]
			break
		}
	}
	if target == nil {
		fail(w, http.StatusNotFound, errors.New("文件不存在"))
		return
	}
	if target.State != hdmap.StateComplete {
		fail(w, http.StatusConflict, errors.New("这个文件还没传完"))
		return
	}

	type segment struct {
		doc    tgclient.Doc
		offset int64
		size   int64
	}
	segments := make([]segment, 0, len(target.MessageIDs))
	for _, messageID := range target.MessageIDs {
		doc, ok := docs[messageID]
		if !ok {
			fail(w, http.StatusConflict, errors.New("缺少分片信息"))
			return
		}
		segments = append(segments, segment{doc: doc, size: doc.Size})
	}

	start, end := int64(0), target.Size-1
	partial := false
	if header := r.Header.Get("Range"); strings.HasPrefix(header, "bytes=") {
		spec := strings.TrimPrefix(header, "bytes=")
		chunks := strings.SplitN(spec, "-", 2)
		if len(chunks) == 2 {
			if chunks[0] != "" {
				if value, err := strconv.ParseInt(chunks[0], 10, 64); err == nil {
					start = value
				}
			}
			if chunks[1] != "" {
				if value, err := strconv.ParseInt(chunks[1], 10, 64); err == nil && value < end {
					end = value
				}
			}
			partial = true
		}
	}
	if start < 0 || start >= target.Size || end < start {
		w.Header().Set("Content-Range", fmt.Sprintf("bytes */%d", target.Size))
		http.Error(w, "range not satisfiable", http.StatusRequestedRangeNotSatisfiable)
		return
	}

	contentType := segments[0].doc.Mime
	if contentType == "" {
		contentType = "application/octet-stream"
	}
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("Accept-Ranges", "bytes")
	w.Header().Set("Content-Disposition",
		fmt.Sprintf("attachment; filename*=UTF-8''%s", urlEscape(target.Name)))
	if partial {
		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", start, end, target.Size))
		w.Header().Set("Content-Length", strconv.FormatInt(end-start+1, 10))
		w.WriteHeader(http.StatusPartialContent)
	} else {
		w.Header().Set("Content-Length", strconv.FormatInt(target.Size, 10))
		w.WriteHeader(http.StatusOK)
	}

	position := int64(0)
	for _, item := range segments {
		partStart, partEnd := position, position+item.size
		position = partEnd
		if partEnd <= start || partStart > end {
			continue
		}
		from := start
		if from < partStart {
			from = partStart
		}
		to := end + 1
		if to > partEnd {
			to = partEnd
		}
		if err := s.tg.ReadRange(r.Context(), item.doc, from-partStart, to-from, w); err != nil {
			log.Printf("download %s: %v", target.Name, err)
			return
		}
	}
}

func urlEscape(value string) string {
	replacer := strings.NewReplacer(" ", "%20", "'", "%27", "(", "%28", ")", "%29")
	return replacer.Replace(value)
}

// ListenAndServe is a convenience wrapper used by main.
func (s *Server) ListenAndServe(addr string) error {
	server := &http.Server{
		Addr:              addr,
		Handler:           s.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	log.Printf("listening on http://%s", addr)
	return server.ListenAndServe()
}

var _ = mime.TypeByExtension
