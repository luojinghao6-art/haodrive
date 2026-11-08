# 风险与免责声明 / Risk & Disclaimer

> 本项目只是个人兴趣爱好研究，不是产品，也不提供服务保证。
> This project is a personal hobby/research experiment, not a product, and comes with no service guarantees.

---

## 中文

### 1. 这是什么

HaoDrive 是一个自托管的实验性项目，用来研究两件事：

1. 把 **Telegram 私有频道当文件存储后端** 是否可行；
2. 浏览器端 **分片上传**、以及跨分片 **Range 播放/拖动** 的实际表现。

它跑在作者自己的 VPS 和测试账号上，用于验证技术方案，不承诺稳定性、安全性或长期可用性。

### 2. 可能违反 Telegram 服务条款

把即时通讯服务的接口当网盘使用，**很可能违反 Telegram 的服务条款（Terms of Service）**。典型的高风险行为包括：

- 批量、自动化地往频道里上传大量文件；
- 长期存储大体积文件，把它当无限量云盘；
- 高频调用 MTProto 接口（上传、下载、转发、删除）；
- 用脚本绕过客户端行为限制。

Telegram 对滥用行为的处置包括但不限于：

- 限制上传/下载速度（FloodWait）；
- 限制账号部分功能；
- 临时冻结账号；
- **永久封禁账号**；
- 连同账号下的频道、群组和文件一起不可访问。

**账号、频道和数据都是你自己的，风险自负。**

### 3. 法律与合规

- 不同国家和地区对数据存储、跨境传输、版权内容的法规不同，**使用前请自行确认当地法律**。
- 不要用它存储、传播违法或侵权内容。
- 不要把它当作有合规要求的正式存储（例如涉密、个人敏感信息、需要审计与留存的数据）。
- 本项目与 Telegram 官方没有任何关系，未获得任何授权或背书。

### 4. 责任限制

作者不对以下后果负责：账号封禁、频道消失、文件丢失、服务中断、数据泄露、以及任何直接或间接的法律责任。**重要数据请务必另行备份。**

### 5. 相对稳妥的用法

- 先用 **测试账号 / 小号** 验证，不要一上来就用主账号；
- 只存自己的文件，控制上传频率和总量；
- 需要长期保存的东西，本地或正规云盘再留一份；
- 遇到 `FloodWait` 就停下来等，不要重试轰炸；
- 不要把这个服务公开给不信任的人使用。

---

## English

### 1. What this is

HaoDrive is a self-hosted experiment that explores two things:

1. whether a **private Telegram channel can serve as a file-storage backend**, and
2. how **browser-side multipart upload** and **cross-part Range playback/seeking** behave in practice.

It runs on the author's own VPS with a test account to validate the approach. Stability, security and long-term availability are not promised.

### 2. Possible violation of Telegram's Terms of Service

Using a messaging service's API as a cloud drive **may well violate Telegram's Terms of Service**. Typical high-risk behaviours include:

- bulk or automated uploads into channels;
- long-term storage of large files, treating it as an unlimited drive;
- high-frequency MTProto calls (upload, download, forward, delete);
- scripting around client-side limits.

Telegram's responses to abuse can include, but are not limited to:

- rate limiting (FloodWait);
- restriction of some account features;
- temporary freezes;
- **permanent account bans**;
- loss of access to the account's channels, groups and files.

**The account, the channels and the data are yours; the risk is yours too.**

### 3. Legal and compliance

- Laws on data storage, cross-border transfer and copyrighted content vary by jurisdiction. **Check your local law before use.**
- Do not use it to store or distribute illegal or infringing content.
- Do not treat it as compliant storage for regulated data (secrets, sensitive personal data, data that must be audited or retained).
- This project is not affiliated with, authorised by, or endorsed by Telegram.

### 4. Limitation of liability

The author is not responsible for account bans, lost channels, lost files, service interruptions, data leaks, or any direct or indirect legal consequences. **Back up anything important elsewhere.**

### 5. Safer use

- Validate with a **test / secondary account** first, not your main one;
- store only your own files and keep upload rates and volume modest;
- keep another copy of anything you care about locally or on a regular cloud drive;
- when you hit `FloodWait`, stop and wait instead of retrying in a loop;
- do not expose the service to people you do not trust.
