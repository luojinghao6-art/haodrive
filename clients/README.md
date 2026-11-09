# HaoDrive 独立客户端 / Standalone clients

三个**不依赖服务器**的图形客户端在这里实现。方案见 [../docs/CLIENTS.md](../docs/CLIENTS.md)（中英双语）。

Three **server-free** GUI clients live here. See [../docs/CLIENTS.md](../docs/CLIENTS.md) for the plan (bilingual).

```
clients/
  spec/                跨语言规范：测试样本 + 格式说明（Go 和 Kotlin 都必须过）
  desktop/             Windows exe + Linux 二进制（Go）
    cmd/hdc/           无网络的命令行工具：从消息 JSON 重建文件列表
    internal/hdmap/    映射规则实现（解析 caption、拼分组、map.txt 覆盖层）
  android/             Android apk（Kotlin + Compose，M4 开始）
```

## 现在到哪一步了

| 里程碑 | 状态 |
|---|---|
| M0 规范 + 测试样本 | ✅ `spec/vectors.json`，Go 侧全部通过 |
| M1 Go 核心（映射层） | ✅ `internal/hdmap` + `hdc` 命令行 |
| M1 Go 核心（Telegram 传输） | ✅ 登录/列频道/上传切片/按 Range 下载（代码完成，待真账号验证） |
| M2 Windows GUI | ✅ 本地 Web UI + `HaoDrive.exe` |
| M3 Linux 二进制 | ✅ 交叉编译单文件 |
| M4 Android | ⬜ 需要 TDLib，下一轮 |
| M5 map.txt 覆盖层 | ✅ 解析/合并/应用已实现并测试（UI 尚未接） |

## 跑起来

```bash
cd clients/desktop
go test ./...                 # 跑 spec/vectors.json 里的全部样本
go build -o hdc ./cmd/hdc
./hdc rebuild ../spec/example-messages.json
./hdc rebuild --overlay map.txt ../spec/example-messages.json
```

`hdc` 不联网。它读一份消息 JSON，打印客户端会看到哪些逻辑文件——用来手动排查某个频道，也用来给 Kotlin 端做对照。

## 规则的两个来源

1. **规范**：[docs/CLIENTS.md](../docs/CLIENTS.md) 第 7、8 节
2. **可执行样本**：[spec/vectors.json](spec/vectors.json) —— 文档和实现打架时以它为准，新增规则必须先加样本
