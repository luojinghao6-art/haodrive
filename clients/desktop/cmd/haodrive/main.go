// Command haodrive is the standalone desktop client.
//
// It serves a local web UI on 127.0.0.1 and talks to Telegram directly: no
// HaoDrive server, no VPS in the middle.  The Telegram session and the saved
// api_id/api_hash live under the data directory.
package main

import (
	"flag"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"time"

	"github.com/luojinghao6-art/haodrive/clients/desktop/internal/server"
	"github.com/luojinghao6-art/haodrive/clients/desktop/internal/tgclient"
)

func main() {
	listen := flag.String("listen", "127.0.0.1:8787", "本地监听地址（只支持回环）")
	dataDir := flag.String("data", defaultDataDir(), "数据目录（Telegram 会话、配置）")
	openBrowser := flag.Bool("open", true, "启动后自动打开浏览器")
	flag.Parse()

	log.SetFlags(log.LstdFlags)
	if err := os.MkdirAll(*dataDir, 0o700); err != nil {
		log.Fatalf("cannot create data dir: %v", err)
	}
	log.Printf("data dir: %s", *dataDir)

	client := tgclient.New(*dataDir)
	httpServer := server.New(client, *dataDir)
	url := "http://" + *listen + "/"

	go func() {
		if err := httpServer.ListenAndServe(*listen); err != nil {
			log.Fatalf("server stopped: %v", err)
		}
	}()

	// Give the listener a moment before the browser tries to connect.
	time.Sleep(300 * time.Millisecond)
	if *openBrowser {
		if err := launch(url); err != nil {
			log.Printf("打不开浏览器，手动访问 %s", url)
		}
	}
	log.Printf("HaoDrive 已启动：%s", url)
	select {}
}

func defaultDataDir() string {
	if base, err := os.UserConfigDir(); err == nil {
		return filepath.Join(base, "HaoDrive")
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".haodrive")
}

func launch(url string) error {
	switch runtime.GOOS {
	case "windows":
		return exec.Command("rundll32", "url.dll,FileProtocolHandler", url).Start()
	case "darwin":
		return exec.Command("open", url).Start()
	default:
		return exec.Command("xdg-open", url).Start()
	}
}
