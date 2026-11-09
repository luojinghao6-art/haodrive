// Package tgclient talks to Telegram directly with gotd/td.
//
// One instance owns one login session.  The HTTP layer creates it, shows the
// login steps, and then uses it to list channels, list messages, upload parts
// and read byte ranges back.  No HaoDrive server is involved.
package tgclient

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"math/rand"
	"mime"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/gotd/td/session"
	"github.com/gotd/td/telegram"
	"github.com/gotd/td/telegram/auth"
	"github.com/gotd/td/telegram/uploader"
	"github.com/gotd/td/tg"

	"github.com/luojinghao6-art/haodrive/clients/desktop/internal/hdmap"
)

// DefaultPartBytes mirrors the server: stay just under Telegram's 2GB cap.
const DefaultPartBytes = int64(1900000000)

// State is what the UI polls while logging in.
type State struct {
	Status string `json:"status"` // idle|starting|awaiting_code|awaiting_password|ready|error
	Error  string `json:"error,omitempty"`
	User   string `json:"user,omitempty"`
	Phone  string `json:"phone,omitempty"`
}

// Channel is a Telegram channel used as a folder.
type Channel struct {
	ID         int64  `json:"id"`
	AccessHash int64  `json:"access_hash"`
	Title      string `json:"title"`
	Username   string `json:"username,omitempty"`
}

// Doc is the part of a document the downloader needs.
type Doc struct {
	ID            int64
	AccessHash    int64
	FileReference []byte
	Size          int64
	Name          string
	Mime          string
}

// Item is one channel message that carries a document.
type Item struct {
	Message hdmap.Message
	Doc     *Doc
}

// Client owns the running Telegram connection.
type Client struct {
	mu      sync.Mutex
	dir     string
	client  *telegram.Client
	api     *tg.Client
	runCtx  context.Context
	state   State
	phone   string
	codeCh  chan string
	passCh  chan string
	started bool
	cancel  context.CancelFunc
	cache   map[int64]Channel
}

// New returns a client whose session lives under dir.
func New(dir string) *Client {
	return &Client{dir: dir, cache: map[int64]Channel{}, state: State{Status: "idle"}}
}

// State returns a copy of the current login state.
func (c *Client) State() State {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.state
}

func (c *Client) setStatus(status string) {
	c.mu.Lock()
	c.state.Status = status
	c.mu.Unlock()
}

func (c *Client) setError(err error) {
	c.mu.Lock()
	c.state.Status = "error"
	c.state.Error = err.Error()
	c.mu.Unlock()
}

// Start connects and, if there is no session yet, drives the login flow.
// It returns immediately; poll State() for progress.
func (c *Client) Start(ctx context.Context, apiID int, apiHash, phone string) error {
	c.mu.Lock()
	if c.started {
		c.mu.Unlock()
		return errors.New("已经在连接了")
	}
	c.started = true
	c.phone = phone
	c.codeCh = make(chan string, 1)
	c.passCh = make(chan string, 1)
	c.state = State{Status: "starting", Phone: phone}
	if err := os.MkdirAll(c.dir, 0o700); err != nil {
		c.mu.Unlock()
		return err
	}
	client := telegram.NewClient(apiID, apiHash, telegram.Options{
		SessionStorage: &session.FileStorage{Path: filepath.Join(c.dir, "telegram.session")},
		NoUpdates:      true,
		Device: telegram.DeviceConfig{
			DeviceModel:   "HaoDrive",
			SystemVersion: "desktop",
			AppVersion:    "1.0",
		},
	})
	c.client = client
	runCtx, cancel := context.WithCancel(ctx)
	c.cancel = cancel
	c.mu.Unlock()

	go c.run(runCtx)
	return nil
}

func (c *Client) run(ctx context.Context) {
	err := c.client.Run(ctx, func(runCtx context.Context) error {
		c.mu.Lock()
		c.api = c.client.API()
		c.runCtx = runCtx
		c.mu.Unlock()

		flow := auth.NewFlow(&authenticator{c: c}, auth.SendCodeOptions{})
		if err := c.client.Auth().IfNecessary(runCtx, flow); err != nil {
			c.setError(err)
			return err
		}
		me, err := c.client.Self(runCtx)
		if err != nil {
			c.setError(err)
			return err
		}
		c.mu.Lock()
		c.state.Status = "ready"
		c.state.Error = ""
		c.state.User = strings.TrimSpace(me.FirstName + " " + me.LastName)
		c.mu.Unlock()
		log.Printf("telegram: logged in as %s (id %d)", c.state.User, me.ID)
		<-runCtx.Done()
		return runCtx.Err()
	})
	if err != nil && !errors.Is(err, context.Canceled) {
		c.setError(err)
	}
	c.mu.Lock()
	c.api = nil
	c.started = false
	if c.state.Status != "error" {
		c.state.Status = "idle"
	}
	c.mu.Unlock()
}

// SubmitCode feeds the login code the user typed.
func (c *Client) SubmitCode(code string) error {
	c.mu.Lock()
	ch := c.codeCh
	c.mu.Unlock()
	if ch == nil {
		return errors.New("现在不需要验证码")
	}
	select {
	case ch <- code:
		return nil
	default:
		return errors.New("已经提交过了")
	}
}

// SubmitPassword feeds the two-step password.
func (c *Client) SubmitPassword(password string) error {
	c.mu.Lock()
	ch := c.passCh
	c.mu.Unlock()
	if ch == nil {
		return errors.New("现在不需要两步验证密码")
	}
	select {
	case ch <- password:
		return nil
	default:
		return errors.New("已经提交过了")
	}
}

// Logout stops the connection and forgets the local session file.
func (c *Client) Logout() error {
	c.mu.Lock()
	cancel := c.cancel
	c.cancel = nil
	c.mu.Unlock()
	if cancel != nil {
		cancel()
		// Let Run() finish its own cleanup before the session file goes away.
		time.Sleep(300 * time.Millisecond)
	}
	c.mu.Lock()
	c.started = false
	c.state = State{Status: "idle"}
	c.api = nil
	c.cache = map[int64]Channel{}
	c.mu.Unlock()
	err := os.Remove(filepath.Join(c.dir, "telegram.session"))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

type authenticator struct{ c *Client }

func (a *authenticator) Phone(ctx context.Context) (string, error) { return a.c.phone, nil }

func (a *authenticator) Code(ctx context.Context, sent *tg.AuthSentCode) (string, error) {
	a.c.setStatus("awaiting_code")
	select {
	case code := <-a.c.codeCh:
		return code, nil
	case <-ctx.Done():
		return "", ctx.Err()
	}
}

func (a *authenticator) Password(ctx context.Context) (string, error) {
	a.c.setStatus("awaiting_password")
	select {
	case password := <-a.c.passCh:
		return password, nil
	case <-ctx.Done():
		return "", ctx.Err()
	}
}

func (a *authenticator) AcceptTermsOfService(ctx context.Context, tos tg.HelpTermsOfService) error {
	return nil
}

func (a *authenticator) SignUp(ctx context.Context) (auth.UserInfo, error) {
	return auth.UserInfo{}, errors.New("客户端不支持注册新账号")
}

func (c *Client) ready() (*tg.Client, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.api == nil || c.state.Status != "ready" {
		return nil, errors.New("Telegram 还没登录")
	}
	return c.api, nil
}

// Channels lists the channels (folders) the account is in.
func (c *Client) Channels(ctx context.Context) ([]Channel, error) {
	api, err := c.ready()
	if err != nil {
		return nil, err
	}
	resp, err := api.MessagesGetDialogs(ctx, &tg.MessagesGetDialogsRequest{
		Limit:      100,
		OffsetPeer: &tg.InputPeerEmpty{},
	})
	if err != nil {
		return nil, err
	}
	var chats []tg.ChatClass
	switch value := resp.(type) {
	case *tg.MessagesDialogs:
		chats = value.Chats
	case *tg.MessagesDialogsSlice:
		chats = value.Chats
	default:
		return nil, nil
	}
	out := make([]Channel, 0, len(chats))
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, chat := range chats {
		channel, ok := chat.(*tg.Channel)
		if !ok {
			continue
		}
		entry := Channel{ID: channel.ID, AccessHash: channel.AccessHash, Title: channel.Title,
			Username: channel.Username}
		out = append(out, entry)
		c.cache[channel.ID] = entry
	}
	return out, nil
}

func (c *Client) peer(ctx context.Context, channelID int64) (tg.InputPeerClass, error) {
	c.mu.Lock()
	channel, ok := c.cache[channelID]
	c.mu.Unlock()
	if !ok {
		if _, err := c.Channels(ctx); err != nil {
			return nil, err
		}
		c.mu.Lock()
		channel, ok = c.cache[channelID]
		c.mu.Unlock()
		if !ok {
			return nil, fmt.Errorf("找不到频道 %d", channelID)
		}
	}
	return &tg.InputPeerChannel{ChannelID: channel.ID, AccessHash: channel.AccessHash}, nil
}

// History fetches recent messages with documents, newest first.
func (c *Client) History(ctx context.Context, channelID int64, limit int) ([]Item, error) {
	api, err := c.ready()
	if err != nil {
		return nil, err
	}
	peer, err := c.peer(ctx, channelID)
	if err != nil {
		return nil, err
	}
	if limit <= 0 {
		limit = 500
	}
	items := make([]Item, 0, limit)
	offset := 0
	for len(items) < limit {
		batch := 100
		resp, err := api.MessagesGetHistory(ctx, &tg.MessagesGetHistoryRequest{
			Peer:     peer,
			OffsetID: offset,
			Limit:    batch,
		})
		if err != nil {
			return nil, err
		}
		messages := messagesOf(resp)
		if len(messages) == 0 {
			break
		}
		for _, entry := range messages {
			message, ok := entry.(*tg.Message)
			if !ok {
				continue
			}
			offset = message.ID
			media, ok := message.Media.(*tg.MessageMediaDocument)
			if !ok {
				continue
			}
			document, ok := media.Document.(*tg.Document)
			if !ok {
				continue
			}
			items = append(items, Item{
				Message: hdmap.Message{
					ID:       int64(message.ID),
					FileName: documentName(document),
					Size:     document.Size,
					Caption:  message.Message,
				},
				Doc: &Doc{
					ID:            document.ID,
					AccessHash:    document.AccessHash,
					FileReference: document.FileReference,
					Size:          document.Size,
					Name:          documentName(document),
					Mime:          document.MimeType,
				},
			})
		}
		if len(messages) < batch {
			break
		}
	}
	return items, nil
}

// Files rebuilds the logical file list for a channel.
func (c *Client) Files(ctx context.Context, channelID int64, limit int) ([]hdmap.File, map[int64]Doc, error) {
	items, err := c.History(ctx, channelID, limit)
	if err != nil {
		return nil, nil, err
	}
	messages := make([]hdmap.Message, 0, len(items))
	docs := make(map[int64]Doc, len(items))
	for _, item := range items {
		messages = append(messages, item.Message)
		if item.Doc != nil {
			docs[item.Message.ID] = *item.Doc
		}
	}
	return hdmap.Rebuild(messages), docs, nil
}

// UploadFile sends one logical file, sliced into Telegram parts.
func (c *Client) UploadFile(ctx context.Context, channelID int64, path, name string, size int64,
	onProgress func(sent, total int64)) ([]int, error) {
	api, err := c.ready()
	if err != nil {
		return nil, err
	}
	peer, err := c.peer(ctx, channelID)
	if err != nil {
		return nil, err
	}
	handle, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer handle.Close()

	sizes := hdmap.PlanParts(size, DefaultPartBytes)
	ids := make([]int, 0, len(sizes))
	var offset int64
	for index, partSize := range sizes {
		partName := hdmap.PartName(name, index+1, len(sizes))
		caption := ""
		if len(sizes) > 1 {
			caption = hdmap.FormatCaption(index+1, len(sizes))
		}
		section := io.NewSectionReader(handle, offset, partSize)
		upload := uploader.NewUpload(partName, section, partSize)
		file, err := uploader.NewUploader(api).WithPartSize(512*1024).Upload(ctx, upload)
		if err != nil {
			return ids, fmt.Errorf("上传第 %d 片失败: %w", index+1, err)
		}
		updates, err := api.MessagesSendMedia(ctx, &tg.MessagesSendMediaRequest{
			Peer: peer,
			Media: &tg.InputMediaUploadedDocument{
				File:      file,
				MimeType:  mimeOf(partName),
				ForceFile: true,
				Attributes: []tg.DocumentAttributeClass{
					&tg.DocumentAttributeFilename{FileName: partName},
				},
			},
			Message:  caption,
			RandomID: rand.Int63(),
		})
		if err != nil {
			return ids, fmt.Errorf("发送第 %d 片失败: %w", index+1, err)
		}
		id := messageIDOf(updates)
		if id == 0 {
			return ids, errors.New("Telegram 没有返回消息 id")
		}
		ids = append(ids, id)
		offset += partSize
		if onProgress != nil {
			onProgress(offset, size)
		}
	}
	return ids, nil
}

// ReadRange streams length bytes of a document starting at offset.
func (c *Client) ReadRange(ctx context.Context, doc Doc, offset, length int64, out io.Writer) error {
	api, err := c.ready()
	if err != nil {
		return err
	}
	location := &tg.InputDocumentFileLocation{
		ID:            doc.ID,
		AccessHash:    doc.AccessHash,
		FileReference: doc.FileReference,
	}
	const chunk = 512 * 1024
	for done := int64(0); done < length; {
		want := int64(chunk)
		if remaining := length - done; remaining < want {
			want = remaining
		}
		resp, err := api.UploadGetFile(ctx, &tg.UploadGetFileRequest{
			Location: location,
			Offset:   offset + done,
			Limit:    int(want),
		})
		if err != nil {
			return err
		}
		file, ok := resp.(*tg.UploadFile)
		if !ok || len(file.Bytes) == 0 {
			break
		}
		if _, err := out.Write(file.Bytes); err != nil {
			return err
		}
		done += int64(len(file.Bytes))
	}
	return nil
}

func messagesOf(resp tg.MessagesMessagesClass) []tg.MessageClass {
	switch value := resp.(type) {
	case *tg.MessagesMessages:
		return value.Messages
	case *tg.MessagesChannelMessages:
		return value.Messages
	case *tg.MessagesMessagesSlice:
		return value.Messages
	default:
		return nil
	}
}

func documentName(document *tg.Document) string {
	for _, attribute := range document.Attributes {
		if name, ok := attribute.(*tg.DocumentAttributeFilename); ok {
			return name.FileName
		}
	}
	return fmt.Sprintf("file-%d", document.ID)
}

func mimeOf(name string) string {
	if value := mime.TypeByExtension(filepath.Ext(name)); value != "" {
		return value
	}
	return "application/octet-stream"
}

func messageIDOf(updates tg.UpdatesClass) int {
	var list []tg.UpdateClass
	switch value := updates.(type) {
	case *tg.Updates:
		list = value.Updates
	case *tg.UpdatesCombined:
		list = value.Updates
	case *tg.UpdateShortSentMessage:
		return value.ID
	}
	for _, update := range list {
		switch event := update.(type) {
		case *tg.UpdateNewChannelMessage:
			if message, ok := event.Message.(*tg.Message); ok {
				return message.ID
			}
		case *tg.UpdateNewMessage:
			if message, ok := event.Message.(*tg.Message); ok {
				return message.ID
			}
		}
	}
	return 0
}
