# QQ_BOT v1.1.0

面向 Linux 无界面服务器的 QQ 群机器人，通过 NapCat / OneBot 11 正向 WebSocket 工作。支持 Python 3.11–3.13，运行时仅依赖 aiohttp。本地目录可命名为 `QQ-bot`。

## 功能

- 群成员真正 @ 机器人后触发 AI 问答；不主动回复未 @ 的消息。
- 按群保存有限的文字、图片及引用上下文，提供用户冷却、群回复间隔和每小时回复上限。
- AI 使用兼容 Anthropic Messages 的接口；可选服务端原生联网搜索。
- `@机器人 点歌 歌名`：网易云搜索与音乐卡片。
- `@机器人 画图 提示词`：NovelAI，支持 Full / Curated 模型选择、异步生成及发送确认。
- 自动断线重连；Linux 下按配置文件路径加锁，支持 Ctrl+C 和 SIGTERM 停止。

v1.1.0 已移除桌面控制台、控制器、VBS 启动器、界面资源、旧测试目录、备份及失效部署文件。版本变更见 [CHANGELOG.md](CHANGELOG.md)。

## 前置条件与安装

先部署并登录 NapCat 或其他兼容 OneBot 11 的客户端，启用**正向 WebSocket 服务**。机器人主动连接该服务；反向 WebSocket、HTTP 地址不能作为 `ws_url`。认证 Token 必须与客户端一致，无需认证时可留空。

从本仓库 Releases 下载 `QQ_BOT-v1.1.0.tar.gz` 或 ZIP，与 `SHA256SUMS.txt` 放在同一目录。Linux 示例：

```sh
sha256sum --check --ignore-missing SHA256SUMS.txt
tar -xzf QQ_BOT-v1.1.0.tar.gz
cd QQ_BOT
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# 下载包自带全空 config.json；克隆源码时仅在文件不存在时复制模板。
test -e config.json || cp config.example.json config.json
chmod 600 config.json
```

按下文填写运行配置，然后前台启动：

```sh
.venv/bin/python bot.py
```

Ctrl+C 停止；服务运行时使用 `systemctl stop qq-bot`。修改配置后重启。Windows 命令行可用 `.\.venv\Scripts\python.exe bot.py`，无需桌面面板。

## 配置

`config.example.json` 的完整字段全部留空：字符串为 `""`，数组为 `[]`，数字和布尔值为 `null`。下载包的 `config.json` 使用相同空白内容。下表中的默认值由程序在内存中提供，不会写回配置。

**启动必填**：`ws_url`、非空 `allowed_groups`、`ai.base_url`、`ai.model` 及 AI 密钥（配置或环境变量）。空群名单禁止启动，也不会开放所有群。群号填写为数字字符串。所有功能只在顶层名单内的群执行。

| 字段 | 含义与空值行为 |
| --- | --- |
| `ws_url` | OneBot 正向 WebSocket 地址，必填，支持 ws / wss。 |
| `access_token` | OneBot 认证 Token；按客户端要求填写，允许为空。 |
| `allowed_groups` | 可使用机器人功能的群名单，必须非空。 |
| `cooldown_seconds` | 每用户请求冷却，默认 5 秒；允许 0–3600 秒。 |
| `at_sender` | 是否 @ 发起者，默认开启。 |
| `ai.base_url` | 兼容 Anthropic Messages 的服务地址，必填；不再使用 OpenAI Chat Completions。完整 `/messages` 地址直接使用；以 `/v1` 结尾时补 `/messages`，其他基础地址补 `/anthropic/v1/messages`。DeepSeek 的 `/v1` 地址使用 `/anthropic/v1/messages`。 |
| `ai.api_key` | AI 密钥，必填；留空时尝试环境变量。 |
| `ai.model` | 服务端支持的模型名，必填，不预设具体模型。 |
| `ai.system_prompt` | 系统提示词；留空使用简洁、直接回答群成员问题的通用提示。 |
| `ai.timeout_seconds` | AI 超时，默认 60 秒；范围 1–300。 |
| `ai.max_tokens` | 输出 Token 上限，默认 500；范围 1–8192。 |
| `ai.max_input_chars` | 当前输入字符上限，默认 2000；范围 1–10000。 |
| `ai.max_reply_chars` | 回复字符上限，默认 1000；范围 1–4000。 |
| `ai.error_reply` | AI 失败时的公开提示，留空使用通用错误提示。 |
| `context.context_messages` | 每群最多上下文消息，默认 20；范围 1–100。 |
| `context.context_minutes` | 上下文保留时间，默认 15 分钟；范围 1–1440。 |
| `context.context_max_chars` | 上下文文本总预算，默认 4000；范围 100–20000。 |
| `context.context_max_images` | 上下文图片上限，默认 3；范围 0–10，0 关闭图片上下文。 |
| `reply_limits.min_interval_seconds` | 群 AI 回复间隔，默认 30 秒；范围 0–3600。 |
| `reply_limits.max_replies_per_hour` | 每群每小时 AI 回复上限，默认 100；范围 1–1000。 |
| `music.enabled` | 点歌开关；空值关闭，显式设为 true 开启。 |
| `music.search_url` | 搜索接口，留空使用网易云公开搜索地址。 |
| `music.timeout_seconds` | 搜索超时，默认 10 秒；范围 1–60。 |
| `music.max_query_chars` | 歌名长度上限，默认 100；范围 1–200。 |
| `web_search.enabled` | 原生联网搜索开关；空值关闭。需服务端支持 `web_search_20250305`，不使用本地搜索密钥或旧搜索模块。 |
| `novelai_image_generation.enabled` | 画图开关；空值关闭，开启时必须填写 Token。 |
| `novelai_image_generation.token` | NovelAI Persistent API Token；开启画图时必填。 |
| `novelai_image_generation.allowed_groups` | **Full 模型选择名单**；名单内使用 `nai-diffusion-5-full`，其他已获顶层授权的群使用 `nai-diffusion-5-curated`。此字段不会扩大顶层功能权限；空数组全部使用 Curated。 |
| `novelai_image_generation.width` / `height` | 默认 832×1216；每边 64–2048 且为 64 的倍数，总像素最多 2097152。 |
| `novelai_image_generation.steps` | 默认 23；范围 1–50。 |
| `novelai_image_generation.guidance` | 默认 7；范围 0–10。 |
| `novelai_image_generation.sampler` | 默认 `k_euler_ancestral`；支持 Euler、Euler Ancestral 和代码中的 DPM++ 采样器。 |
| `novelai_image_generation.timeout_seconds` | 画图超时，默认 180 秒；范围 1–300。 |

画图提示词支持多行与权重，最多 4000 字符，同一进程同时生成一张图片。生成和图片发送期间收到重复请求会提示等待；发送确认超时不会重新生成或重复发送。

## 环境变量

| 名称 | 用途 |
| --- | --- |
| `BOT_CONFIG` | 配置文件路径，默认当前工作目录的 `config.json`；推荐服务器使用绝对路径。 |
| `ONEBOT_WS_URL` | 覆盖 `ws_url`。 |
| `ONEBOT_ACCESS_TOKEN` | 覆盖 `access_token`，可显式设为空。 |
| `AI_API_KEY` / `DEEPSEEK_API_KEY` | 配置密钥为空时依次读取。 |
| `LOG_LEVEL` | 默认 INFO；仅记录状态、错误类别或配置字段名，不记录消息、群号、密钥、连接地址和接口响应。 |
| `MUSIC_SIGN_URL` | 可选音乐卡片签名服务地址；未设置时使用标准 OneBot 网易云音乐段。需确认该服务可信并接受音乐元数据。 |
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | NovelAI 请求使用的代理设置。 |

点歌实际可用性取决于网易云接口和 OneBot 客户端的音乐卡片支持，项目不内置私人签名服务器。

## systemd 部署

以下示例使用普通用户 `qqbot`，代码位于 `/opt/QQ_BOT`，外部配置位于 `/etc/qq-bot/config.json`。安装依赖及修改服务文件需要管理员权限，机器人进程使用普通用户。

```sh
sudo useradd --system --create-home --shell /usr/sbin/nologin qqbot
sudo install -d -o qqbot -g qqbot -m 700 /etc/qq-bot
sudo install -o qqbot -g qqbot -m 600 config.example.json /etc/qq-bot/config.json
# 编辑 /etc/qq-bot/config.json，填写必需字段，再启动。
sudo cp deploy/qq-bot.service /etc/systemd/system/qq-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now qq-bot
sudo systemctl status qq-bot
```

上述安装配置命令仅用于首次部署；已有配置时不要再次覆盖。`/opt/QQ_BOT/.venv/bin/python` 和代码需能被 qqbot 读取执行。服务示例限制写入到配置目录；该目录必须允许服务用户创建 `config.json.lock`。锁文件保留在磁盘，进程停止后系统自动释放锁，不必手动删除。

```sh
sudo systemctl stop qq-bot
sudo systemctl restart qq-bot
sudo journalctl -u qq-bot -f
```

## 从 v1.0.0 升级

停止旧进程，将新版本解压到新目录，保留原配置（建议放在项目之外），安装新依赖，再切换服务路径并重启。不要把下载包的空白 `config.json` 覆盖到已有配置。

- AI 服务必须支持 Anthropic Messages；检查 `ai.base_url` 和 `ai.model`，只有 Chat Completions 的服务不能直接使用。
- 顶层群名单必须明确填写。画图名单在此版本用于选择 Full 模型，其余顶层授权群使用 Curated；不再沿用 v1.0.0 的独立画图授权含义。
- 新字段为 `context` 和 `reply_limits`。旧 `auto_reply` 中的上下文及限流参数只在内存中兼容读取，主动聊天开关不再生效。
- 旧 `web_search` 的供应商及密钥字段不再使用；保留的 `enabled` 控制服务端原生搜索。

## 故障排查与隐私

启动失败先检查必填字段、JSON 类型、环境变量和配置目录权限；程序不输出具体敏感值。退出码 1 表示配置或启动错误，2 表示同配置已有实例。不要对正在使用的锁文件执行删除。

连接失败检查 NapCat 是否已登录、正向 WebSocket 是否开启、网络和 Token 是否一致。AI 失败检查 Messages 接口、模型、额度和联网工具支持。画图失败检查 Token、额度、代理和系统时间。点歌失败检查音乐接口、客户端音乐卡片支持及显式配置的签名服务。

群聊上下文只保存在内存，重启清空；选中的文字和图片会发送给配置的 AI 服务，画图提示词发送给 NovelAI 并消耗账户额度。本机配置、备份、日志和虚拟环境均不提交、不打包；GitHub 平台会显示仓库所属账号，历史提交仍由平台保留。
