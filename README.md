# QQ_BOT

面向 Linux 无界面服务器的 QQ 群机器人，通过 NapCat / OneBot 11 正向 WebSocket 接收消息。支持 Python 3.11、3.12、3.13，不需要桌面、Tkinter 或可视化控制面板。
目前点歌功能在服务器端可能暂时无法使用，但不影响其他功能正常运行。

## 功能

- 在明确指定的聊天群中回答真正 `@` 机器人的消息；可选按概率主动参与对话。
- 每群独立的短期图文上下文、消息去重、用户冷却及群级频率限制。
- 可选网易云音乐点歌、DeepSeek 服务端联网搜索、NovelAI 画图。
- 自动重连、Linux 单实例锁、SIGINT / SIGTERM 停止及 systemd 常驻运行。

聊天与点歌使用 `allowed_groups`，画图使用独立的 `novelai_image_generation.allowed_groups`。空名单不开放任何群。

## 前置条件

1. Linux 服务器，已安装 Python 3.11–3.13 及对应的 venv 模块。
2. 已自行安装并登录 NapCat，开启 OneBot 11 的**正向 WebSocket 服务**。本项目作为客户端连接该服务，不是反向 WebSocket 接收端。
3. 可用的 DeepSeek Responses API 服务、模型和 API Key。普通聊天请求使用配置地址的 `/responses` 接口；并非所有仅兼容 Chat Completions 的服务都可使用。
4. 服务器能连接 NapCat 和所开启功能的外部接口。画图需要额外的 NovelAI Persistent API Token、可用订阅或 Anlas 额度。

NapCat 请参考其[官方项目](https://github.com/NapNeko/NapCatQQ)。同机连接优先使用回环地址；跨主机连接时配置受保护的网络和认证。

## 安装与首次配置

从 GitHub Release 下载 `QQ_BOT-v1.0.0.tar.gz`，先用同页的 `SHA256SUMS.txt` 校验下载文件，再解压并进入 `QQ_BOT`：

```sh
sha256sum --ignore-missing -c SHA256SUMS.txt
tar -xzf QQ_BOT-v1.0.0.tar.gz
cd QQ_BOT
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Release 下载包包含全空的 `config.json`。从源码仓库获取时，仅在没有已有配置的情况下创建运行配置：

```sh
test -e config.json || cp config.example.json config.json
chmod 600 config.json
```

使用文本编辑器填写配置。**发布配置和模板的所有值都留空**：字符串为 `""`，数组为 `[]`，数字和开关为 `null`。运行时采用下表的通用默认值，不把默认值写回文件。

首次运行必须填写：`ws_url`、至少一个字符串形式的聊天群号、`ai.base_url`、`ai.model`，以及 `ai.api_key` 或密钥环境变量。`access_token` 按 NapCat 的认证配置填写。地址、模型、密钥和聊天名单均不会从程序自动补齐；全空配置无法启动。

开启功能必须把对应 `enabled` 改成 JSON 布尔值 `true`。`null`、缺省开关均按关闭处理；`false` 明确关闭。数字应使用 JSON 数字，不能使用带引号的字符串；群号则必须使用数字字符串。JSON 不支持注释和末尾多余逗号。

## 配置项

表中的默认值属于运行时行为；它们不是发布文件中的预填值。

| 字段 | 说明及空值行为 |
| --- | --- |
| `ws_url` | 必填，NapCat 正向 WebSocket 地址，以 ws 或 wss 协议开头；也可由 `ONEBOT_WS_URL` 提供。 |
| `access_token` | NapCat Token，可由 `ONEBOT_ACCESS_TOKEN` 提供；无认证服务可留空。 |
| `allowed_groups` | 必填，至少一个数字字符串；仅这些群可聊天和点歌。 |
| `cooldown_seconds` | 同一用户触发冷却，默认 5 秒，不能为负数。 |
| `at_sender` | 回复是否 @ 发送者，空值关闭。 |
| `auto_reply.enabled` | 普通消息主动回复开关，空值关闭；关闭不影响直接 @ 问答。 |
| `auto_reply.probability` | 普通消息回复概率，默认 0.1，范围 0–0.5。 |
| `auto_reply.min_interval_seconds` | 每群最短回复间隔，默认 30 秒，范围 0–3600。 |
| `auto_reply.max_replies_per_hour` | 每群每小时回复上限，默认 100，范围 1–1000。 |
| `auto_reply.context_messages` | 内存上下文消息数，默认 20，范围 1–100。 |
| `auto_reply.context_minutes` | 上下文保留时间，默认 15 分钟，范围 1–1440。 |
| `auto_reply.context_max_chars` | 每次请求上下文文字上限，默认 4000，范围 100–20000。 |
| `auto_reply.context_max_images` | 每次请求上下文图片上限，默认 3，范围 0–10。 |
| `music.enabled` | 网易云点歌开关，空值关闭。 |
| `music.search_url` | 空值使用网易云公开歌曲搜索接口。 |
| `music.timeout_seconds` | 搜索超时，默认 10 秒，范围 1–60。 |
| `music.max_query_chars` | 歌名字符上限，默认 100，范围 1–200。 |
| `web_search.enabled` | 联网搜索开关，空值关闭。开启需要 AI 地址为 DeepSeek 官方根地址。 |
| `novelai_image_generation.enabled` | 画图开关，空值关闭。开启时必须填写 Token 和画图群名单。 |
| `novelai_image_generation.allowed_groups` | 画图群的数字字符串数组，独立于聊天群名单；空数组不开放画图。 |
| `novelai_image_generation.token` | Persistent API Token，无需 Bearer 前缀；本项没有环境变量替代入口。 |
| `novelai_image_generation.width` / `height` | 默认 832×1216；各为 64–2048 的 64 倍数，总像素不超过 2097152。 |
| `novelai_image_generation.steps` | 默认 23，范围 1–50。 |
| `novelai_image_generation.guidance` | 默认 7，范围 0–10。 |
| `novelai_image_generation.sampler` | 默认 k_euler_ancestral；另支持 k_euler、k_dpmpp_2m、k_dpmpp_2s_ancestral、k_dpmpp_sde、k_dpmpp_2m_sde。 |
| `novelai_image_generation.timeout_seconds` | 默认 180 秒，范围 1–300。 |
| `ai.base_url` | 必填，提供 Responses API 的 AI 服务根地址。 |
| `ai.api_key` | 必填，或通过 `AI_API_KEY` / `DEEPSEEK_API_KEY` 提供。 |
| `ai.model` | 必填，该接口支持的模型名。 |
| `ai.system_prompt` | 空值采用通用 QQ_BOT 群聊提示词；可填写自定义提示词。 |
| `ai.timeout_seconds` | AI 请求超时，默认 60 秒，范围 1–300。 |
| `ai.max_tokens` | 初次请求输出上限，默认 500，范围 1–8192；输出额度耗尽时以 4096 上限重试一次。 |
| `ai.max_input_chars` | 单条消息输入字符上限，默认 2000，范围 1–10000。 |
| `ai.max_reply_chars` | 回复字符上限，默认 1000，范围 1–4000。 |
| `ai.error_reply` | 空值采用通用的服务暂不可用提示。 |

直接 @ 回复仍受用户冷却、每群间隔及小时上限约束。开启主动回复时，引用机器人历史回复的消息采用 50% 的回复概率。

## 环境变量与前台运行

| 环境变量 | 用途与优先级 |
| --- | --- |
| `BOT_CONFIG` | 指定配置路径；缺省读取 bot.py 同目录的 config.json。 |
| `ONEBOT_WS_URL` | 非空时覆盖配置中的 WebSocket 地址。 |
| `ONEBOT_ACCESS_TOKEN` | 非空时覆盖配置中的 NapCat Token。 |
| `AI_API_KEY` | 仅在 ai.api_key 为空时使用。 |
| `DEEPSEEK_API_KEY` | 仅在配置密钥与 AI_API_KEY 均为空时使用。 |
| `LOG_LEVEL` | 日志级别，默认 INFO；可设 DEBUG。 |
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | NovelAI 请求遵循这些代理变量；更改后重启。 |

```sh
.venv/bin/python bot.py
```

`Ctrl+C` 停止；后台运行时发送 SIGTERM。相同配置路径只能运行一个进程。配置所在目录需要可写，以创建锁文件；锁文件会保留，进程停止或退出后锁自动释放，不要在运行时删除锁文件。服务器版只支持 Linux / POSIX，Windows 可用于运行平台无关的单元测试。

启动失败退出码为 1，重复实例为 2。配置错误只输出错误类别，不输出实际值。查看字段问题时可在可信终端使用下述校验，不会连接外部接口或改写配置：

```sh
.venv/bin/python -c 'from pathlib import Path; from bot import BotConfig; BotConfig.load(Path("config.json")); print("配置校验通过")'
```

## systemd 常驻运行

将发布内容放在 `/opt/QQ_BOT`，将运行配置放在 `/etc/qq-bot/config.json`。使用独立用户；以下命令按首次安装场景编写：

```sh
sudo useradd --system --no-create-home --shell /usr/sbin/nologin qqbot
sudo install -d -o qqbot -g qqbot -m 700 /etc/qq-bot
sudo install -o qqbot -g qqbot -m 600 config.json /etc/qq-bot/config.json
sudo cp deploy/qq-bot.service /etc/systemd/system/qq-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now qq-bot
```

执行前确保 `/opt/QQ_BOT/.venv/bin/python` 已安装依赖，且 qqbot 对程序文件和虚拟环境具有读取与执行权限。先完成配置再启动，避免把全空文件当作可运行配置。已有 `/etc/qq-bot/config.json` 时请保留原文件，切勿执行覆盖配置的 install 命令。

如果使用环境变量，将它们自行填写到 `/etc/qq-bot/environment`，每行一个 `变量名=值`，权限设为 600，所有者设为 qqbot。不要把该文件加入源码仓库。服务单元允许进程写入配置目录以创建锁文件，其余程序目录只读。

```sh
sudo systemctl status qq-bot
sudo journalctl -u qq-bot -f
sudo systemctl restart qq-bot
sudo systemctl stop qq-bot
```

## 群聊指令

使用 QQ 的真正 @ 消息，不是输入机器人昵称：

- `@机器人 问题`：聊天。
- `@机器人 点歌 歌名`：搜索网易云并发送音乐卡片。
- `@机器人 画图 提示词`：生成一张图片；提示词直接发送至 NovelAI，保留换行和权重，不自动翻译或追加质量词。

画图固定使用 NovelAI V5 Full、Heavy 负面词、单张、随机种子。一次仅处理一个画图任务，失败不会自动重画。聊天和点歌可以继续进行。图片发送确认超时不代表图片一定未发送。

## 升级与隐私

停止服务，先备份服务器配置和可选的 environment 文件，再替换代码、更新依赖并重启。不要把新版本中的空白 config.json 覆盖到正在使用的配置；新增字段可以按说明补充，读取配置不会自动迁移或写回。

`configure_chat_group.py` 可交互追加一个聊天群；输入隐藏，写入前创建独立备份，保留画图名单、其他配置及未知字段。空聊天名单会追加第一个群，不代表开放全部群。成功后重启：

```sh
.venv/bin/python configure_chat_group.py --config /etc/qq-bot/config.json
```

本地配置、环境文件、备份、日志及锁文件均不应提交或打包。上下文仅在进程内存中短暂保留，重启清空；决定回复时，选中的文字和图片会发送到配置的 AI 服务。搜索词会发送到 DeepSeek，画图提示词会发送到 NovelAI。程序日志不记录真实群号、消息正文、密钥、完整连接地址或原始接口异常。

## 故障排查

- **启动失败**：检查 JSON 格式、必填字段、群号字符串格式、文件权限和 Python 版本。启用画图需同时有 Token 和画图群名单。
- **持续重连**：检查 NapCat 已登录、正向 WebSocket 已开启、地址端口和 Token 正确，服务器可访问目标主机。
- **群聊不回复**：检查聊天白名单和真实 @，并确认没有处于用户冷却、群间隔或小时上限；普通消息主动回复需显式开启。
- **联网搜索不可用**：检查已显式开启，AI 使用 DeepSeek 官方地址、账号及模型支持对应服务。
- **点歌失败**：检查功能已开启和网易云搜索接口连通；音乐卡片可能被 OneBot 服务拒绝。
- **画图失败**：检查独立白名单、功能开关、Token、额度、代理以及超时；不自动重复生成。
- **重复实例**：停止使用相同配置的旧进程再启动；不要删除正在使用的锁文件。
- **systemd 无法启动**：检查工作目录、虚拟环境路径、服务用户权限；配置错误和重复实例不会自动反复重启。

## 测试与打包

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q .
.venv/bin/python tools/build_release.py
```

打包工具按明确白名单收集文件，并从全空模板重新生成包内配置，不读取运行中的 config.json。产物在 dist 中，包含 ZIP、tar.gz 和 SHA256 校验文件。GitHub Actions 在 Ubuntu 验证 Python 3.11–3.13，包含真实进程锁、模拟 OneBot 通信与停止重连测试。
