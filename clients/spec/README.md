# 映射规则测试样本 / Mapping vectors

这份 JSON 是 Go 和 Kotlin 两端共同的**可执行规范**。规则写在 [../docs/CLIENTS.md](../docs/CLIENTS.md) 第 7、8 节；两边实现都必须让 `vectors.json` 全部通过，加规则先加样本。

This JSON is the **executable spec** shared by the Go and Kotlin implementations. The rules live in [../docs/CLIENTS.md](../docs/CLIENTS.md) sections 7–8; both implementations must pass every vector, and a new rule starts with a new vector.

## 消息输入 / Message input

```json
{ "id": 101, "file_name": "a.bin.part001", "size": 100, "caption": "[TD-PART 1/2]" }
```

| 字段 | 说明 |
|---|---|
| `id` | Telegram 消息 id，频道内单调递增 |
| `file_name` | 文档属性里的文件名 |
| `size` | 文档大小（字节） |
| `caption` | 消息 caption，没有就是空串 |

| Field | Meaning |
|---|---|
| `id` | Telegram message id, monotonic inside a channel |
| `file_name` | The document's file name |
| `size` | Document size in bytes |
| `caption` | Message caption, empty when absent |

## 三种用例 / Three kinds of case

| 数组 | 输入 | 期望 |
|---|---|---|
| `cases` | `messages` | `Rebuild(messages)` 的完整结果 |
| `overlay_cases` | `messages` + `overlay`（map.txt 文本） | `Apply(Rebuild(...))` 的完整结果 |
| `merge_cases` | 两份 overlay 文本 `a` / `b` | `Merge(a,b)` 的 `rev` 和重命名结果 |

| Array | Input | Expected |
|---|---|---|
| `cases` | `messages` | the full `Rebuild(messages)` output |
| `overlay_cases` | `messages` + `overlay` (map.txt text) | the full `Apply(Rebuild(...))` output |
| `merge_cases` | two overlay texts `a` / `b` | `rev` and renames after `Merge(a,b)` |

结果对象就是客户端列表里的一项：

```json
{
  "name": "a.bin",
  "size": 150,
  "parts": 2,
  "group_key": 101,
  "message_ids": [101, 102],
  "state": "complete"
}
```

`order` 和 `renamed` 只由 overlay 产生，没有就省略。

`order` and `renamed` only come from an overlay and are omitted otherwise.

## 加一条样本 / Adding a case

1. 先想清楚规则，写进 `docs/CLIENTS.md` 第 7/8 节
2. 往对应数组里加一个带 `name` 的用例
3. Go：`cd ../desktop && go test ./...`
4. Kotlin：读同一个文件，跑同样的断言

1. Decide the rule and write it into `docs/CLIENTS.md` sections 7–8
2. Add a named case to the right array
3. Go: `cd ../desktop && go test ./...`
4. Kotlin: read the same file and run the same assertions

## 已知边界 / Known limits

- 同名同片数的两次上传只能按「重复分片序号开始新 run」拆；完全交错时拆不开，会显示为未完成
- caption 只有 `[TD-PART i/N]`，没有组 id；跨频道的同名文件互不影响（按频道分别重建）
- 旧消息（v10 之前）没有 caption，一律当单文件

- Two uploads with the same name and part count are separated only by "a repeated part index starts a new run"; fully interleaved uploads cannot be split and show as incomplete
- The caption carries only `[TD-PART i/N]`, no group id; same-named files in different channels never interfere (rebuild per channel)
- Messages from before v10 have no caption and are always treated as single files
