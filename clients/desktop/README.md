# HaoDrive 桌面客户端 / Desktop client

Windows exe 和 Linux 二进制，Go 写的，**不依赖服务端**：自己登录 Telegram，自己上传、下载、拼装分片。

Windows exe and Linux binary written in Go. **No server needed**: it logs into Telegram itself and does its own upload, download and part stitching.

## 构建 / Build

```bash
go build -o HaoDrive.exe ./cmd/haodrive                       # Windows
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -o haodrive ./cmd/haodrive
```

交叉编译不需要 cgo，产物是单个静态文件。

Cross-compiling needs no cgo; the result is one static binary.

## 运行 / Run

```bash
./HaoDrive.exe                    # 默认 127.0.0.1:8787，自动开浏览器
./HaoDrive.exe -listen 127.0.0.1:9000 -data D:\HaoDrive -open=false
```

| 参数 | 说明 |
|---|---|
| `-listen` | 本地监听地址，只支持回环 |
| `-data` | 数据目录，默认 `%AppData%\HaoDrive`（Windows）/ `~/.config/HaoDrive` |
| `-open` | 启动后自动打开浏览器，默认 true |

## 第一次用 / First run

1. 打开页面，填 `api_id` / `api_hash`（my.telegram.org 建的应用，可以和服务端复用同一组）和手机号
2. 收验证码（可能发在 Telegram 客户端里），有一步验证就再输一次
3. 选文件夹（频道）→ 上传 / 下载

`api_id` / `api_hash` 存在 `数据目录/config.json`（0600），Telegram 会话存在 `数据目录/telegram.session`。**这两个文件都是凭据，别外传。**

## 已经能做什么 / What works

- 登录（验证码 + 两步验证），会话落盘，重启不用再登
- 列出账号所在的频道，选一个当文件夹
- 按 `docs/CLIENTS.md` 的规则重建文件列表（含未完成上传的标记）
- 上传：本地选文件 → 服务端没有 → 直接切片发 Telegram（默认每片 1.9GB），自动写 `[TD-PART i/N]`
- 下载：把分片的字节按顺序拼起来，支持 HTTP Range（视频拖动、断点续传）

## 还没做 / Not yet

- `map.txt` 覆盖层（重命名/排序）只在库里，界面上还没接
- 上传进度只有整片的百分比，没有速度显示
- 没有断点续传一个没传完的文件（要重新选文件）
- 没有缩略图/在线播放界面，只能下载后看
- Android 客户端还没开始（需要 TDLib，另起一轮）

## 映射规则

和 `docs/CLIENTS.md` 第 7、8 节完全一致，实现是 `internal/hdmap`，样本在 `../spec/vectors.json`。
