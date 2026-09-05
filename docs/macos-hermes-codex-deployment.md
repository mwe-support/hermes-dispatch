# macOS Hermes + Codex 快速部署手册

本文用于在 macOS 上新装或增量更新 Hermes、Codex CLI、Codex App、QQ 渠道和
持久化兼容插件。每个部门使用独立的 macOS 登录账号；部门内还可用 Hermes profile
建立多个小组专用 Agent。WhatsApp 是否启用是 **per-profile** 设置：新建且不使用
WhatsApp 的 profile 默认关闭，已有 profile 必须保留其当前状态。

本文不是旧 Mac 迁移或备份恢复手册。命令不固定 Hermes、Codex 或插件版本，
始终安装执行时的最新稳定版。

## 1. 部署边界

- 每个 macOS 部门账号独立保存 `~/.codex`、`~/.hermes`、Keychain、会话和消息快照。
- Codex App 可全机安装一次，但每个部门账号必须分别登录并授予 macOS 权限。
- 不同 Gateway 不得同时使用同一套 QQ Bot 身份，否则会抢事件或重复回复。
- 所有配置、插件安装、测试和重启命令都必须明确目标 profile；不要依赖
  `hermes profile use` 的残留状态。
- 不要提交 `.env`、真实手机号、QQ 标识、Bot 密钥或授权凭据。
- Hermes 更新后重新安装最新 hotfix 并执行回归测试；不要修改安装目录作为永久修复。

## 2. 新 Mac 前置安装

管理员先安装 Apple Command Line Tools：

```bash
xcode-select --install
```

安装 Homebrew：

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

新 Mac mini 使用 Apple Silicon，配置 Homebrew PATH：

```bash
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> "$HOME/.zprofile"
eval "$(/opt/homebrew/bin/brew shellenv)"
```

如果是 Intel Mac，把 `/opt/homebrew/bin/brew` 改为 `/usr/local/bin/brew`。

安装 Node.js 和常用工具：

```bash
brew install node git ripgrep
node --version
npm --version
git --version
rg --version
```

Homebrew 可由管理员全机安装一次；每个部门账号仍需在自己的 `~/.zprofile` 中加入
上述 `brew shellenv` 行。

## 3. 每个部门账号安装 Codex

切换到目标部门的 macOS 账号后执行。先确认没有误用管理员账号：

```bash
whoami
echo "$HOME"
```

安装最新 Codex CLI：

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zprofile"
source "$HOME/.zprofile"
command -v codex
codex --version
```

登录该部门自己的 ChatGPT/Codex 账号，并安装或打开 Codex App：

```bash
env -u CODEX_HOME codex login
env -u CODEX_HOME codex login status
env -u CODEX_HOME codex app
```

Codex 的本地状态位于 `CODEX_HOME`，未设置时默认为 `~/.codex`；其中包括
`config.toml`、文件凭据 `auth.json`、历史、日志和缓存。参见
[Codex 官方配置与状态位置说明](https://learn.chatgpt.com/docs/config-file/config-advanced#config-and-state-locations)。

编辑默认 `CODEX_HOME` 的 `~/.codex/config.toml`，保留已有内容并加入默认权限策略：

```toml
approval_policy = "on-request"
approvals_reviewer = "auto_review"
sandbox_mode = "workspace-write"
cli_auth_credentials_store = "keyring"
```

不要在 Hermes 中再设置一套宽松审批策略；Hermes 负责把 Codex 的审批请求转发到
QQ，最终权限边界由 Codex 配置和每次审批决定。

取消 Codex app-server 墙钟限制和 session-project 映射都由 Hermes hotfix 的 `.env`
变量控制，不需要也不应向 `~/.codex/config.toml` 添加私有的 timeout、project 或
thread 配置键。

同一 macOS 账号内的命名 Hermes profile 如需隔离用户相关 MCP，必须使用独立
`CODEX_HOME`；具体设置见第 9、11 节。Codex App 的 macOS 权限属于当前登录账号，
不因 `CODEX_HOME` 改变；Codex 配置、MCP、历史和文件凭据则按 `CODEX_HOME` 隔离。

在“系统设置 → 隐私与安全性”中，按实际启用能力分别为 Codex App/终端批准：

- 辅助功能；
- 屏幕与系统音频录制；
- 自动化；
- 文件与文件夹。

## 4. 安装并初始化 Hermes

```bash
unset HERMES_HOME CODEX_HOME
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
source "$HOME/.zprofile"
command -v hermes
hermes -p default --version
hermes -p default setup
hermes -p default gateway setup
hermes -p default gateway stop
```

先运行向导，让当前 Hermes 生成完整配置结构，再用命令调整。不要直接复制旧版本的
整份 `config.yaml`。

QQ 文件桥接在官方 Hermes 0.20.5 上须使用 **qqbot-connect-hotfix 1.8.24 或更高版本**；
当前应安装 **1.8.25 或更高版本**，同时修复真实输出位于末尾引用/未闭合围栏之前时丢附件的 P2。
1.8.22/1.8.23 错向旧路径校验器传递 `session_key`，会破坏已有 MEDIA 附件。
1.8.24 按实际函数签名兼容官方 `v2026.8.19`（`fcbd1076a`）与
`v2026.8.31`（`29112bef`），不要求为了文件桥接升级 Hermes。开发提交的版本字段
不能代替官方 tag 验证。安装后重启目标 profile，按插件 README 验证普通附件及
代码/引用示例不上传；回退使用安装器打印的备份目录，旧缺陷也会随旧插件恢复。

QQ 官方 C2C 流式消息要求 Hermes **0.20.5 或更高版本**。更早版本不满足该插件的
streaming 兼容契约，不能启用本文的 QQ streaming 设置。先检查实际运行源码版本：

```bash
hermes -p default --version
HERMES_PY="$HOME/.hermes/hermes-agent/venv/bin/python"
"$HERMES_PY" - <<'PY'
import re
from hermes_cli import __version__

match = re.fullmatch(r"\s*(\d+)\.(\d+)\.(\d+)\s*", __version__)
if match is None:
    raise SystemExit(
        f"Hermes {__version__} is not a stable x.y.z release; native QQ "
        "streaming must fail closed"
    )
version = tuple(int(part) for part in match.groups())
if version < (0, 20, 5):
    raise SystemExit(f"Hermes {__version__} is too old; require >= 0.20.5")
print(f"Hermes {__version__}: QQ native streaming compatible")
PY
```

旧环境先查看更新范围和所有 profile 的重启计划，再执行带备份的官方更新：

```bash
hermes -p default update --check
if hermes -p default update --help | rg -q -- '--plan'; then
  hermes -p default update --plan
fi
hermes -p default update --backup
hermes -p default --version
```

版本检查未通过时不要设置 `display.platforms.qqbot.streaming=true`，也不要重启生产
Gateway。`qqbot-connect-hotfix` 1.8.21 在旧版、预发布版或无法识别版本的 Hermes 上会 fail-closed，
不会替换 `send`、`send_typing` 或 Gateway streaming gate。

## 5. 用命令配置 `config.yaml`

以下命令明确写入默认 profile 的 `~/.hermes/config.yaml`。`-p default` 是必要条件：
仅设置 `HERMES_HOME="$HOME/.hermes"` 或清除 `HERMES_HOME` 仍可能被
`~/.hermes/active_profile` 重定向。

```bash
unset CODEX_HOME

hermes -p default config migrate

hermes -p default config set model.provider openai-codex
hermes -p default config set model.base_url https://chatgpt.com/backend-api/codex
hermes -p default config set model.openai_runtime codex_app_server
hermes -p default config set agent.max_turns 150
hermes -p default config set agent.reasoning_effort medium
hermes -p default config set compression.codex_app_server_auto native

hermes -p default config set display.interim_assistant_messages true
hermes -p default config set display.streaming true
hermes -p default config set streaming.enabled true
hermes -p default config set streaming.transport auto
hermes -p default config set display.platforms.qqbot.interim_assistant_messages true
hermes -p default config set display.platforms.qqbot.streaming true
hermes -p default config set display.platforms.qqbot.tool_progress new

hermes -p default config set group_sessions_per_user false
hermes -p default config set session_reset.mode none
hermes -p default config set approvals.mode smart
hermes -p default config set approvals.mcp_reload_confirm false
hermes -p default config set agent.gateway_timeout 7200
hermes -p default config set agent.gateway_timeout_warning 900
hermes -p default config set agent.restart_drain_timeout 300

hermes -p default config set platforms.qqbot.enabled true
hermes -p default config set platforms.qqbot.extra.group_policy open

hermes -p default config check
```

仅对**新建且不使用 WhatsApp** 的 profile 追加：

```bash
hermes -p default config set platforms.whatsapp.enabled false
```

更新已有 profile 时先执行
`hermes -p default config get platforms.whatsapp.enabled`，保留原值；
不得为了部署 QQ hotfix 而统一关闭 WhatsApp。

关键点：

- `qqbot-connect-hotfix` 在兼容的稳定版 Hermes 上让 QQ C2C 私聊使用官方流式消息接口；
  群聊和 QQ 频道私信仍走原有回复路径。它提供可见中间状态并保证 final 不重复；QQ 的
  被动回复窗口或次数耗尽后无法继续向同一条入站消息回复，因此真实验收应及时完成。
  协议、并发和失败恢复边界详见
  [插件 README](../plugins/qqbot-connect-hotfix/README.md)，真实回归证据见
  [PR #4 证据](evidence/pr-4/README.md)。
- `group_sessions_per_user=false` 让同一群共用上下文；审批 hotfix 仍会校验发起人。
- steer 被接受后，QQ 应结束旧气泡并在新气泡继续同一 Hermes session/Codex thread；
  验收必须检查 Codex 实际采用更正并返回新要求的 final，不能只看确认消息。
- `approvals.mode=smart` 让 Hermes 自动判断危险命令：低风险命令可自动放行，不确定的
  请求才发送人工审批；它不替代 Codex app-server 自身的审批策略。
- `agent.gateway_timeout=7200` 将 Gateway 的无活动保护延长到 2 小时；`/stop` 和消息
  interrupt/steer 仍可终止或调整任务。
- `agent.gateway_timeout_warning=900` 会在连续 15 分钟无活动时发送状态提醒；它不是
  Agent final，也不会释放会话。需要减少提醒时可改为 `3600`，设为 `0` 则关闭提醒。
- `agent.restart_drain_timeout=300` 是独立的重启排空窗口。Hermes 0.20.0 默认值为 0，
  显式重启会立即强制结束其他会话；设为 300 后先等待最多 5 分钟再强制退出。
- 标量使用 `hermes config set`。工具集列表使用第 7 节的 `hermes tools enable`，不要把
  JSON 字符串写进 `platform_toolsets`。

审批历史积累后，可生成命令 allowlist 建议。默认只展示建议，不写入配置：

```bash
hermes -p default approvals suggest
```

人工审核编号后，再选择性应用，例如：

```bash
hermes -p default approvals suggest --apply 1,2
```

`suggest` 是 `hermes approvals` 的子命令，不是 `approvals.mode` 的取值；破坏性命令
不会被加入建议列表。

## 6. 配置 `.env`

新装默认 profile 时，明确打开它的环境文件：

```bash
DEFAULT_ENV="$(hermes -p default config env-path)"
nano "$DEFAULT_ENV"
```

加入以下模板并替换占位值。已有 profile 只补充或修改明确列出的非凭据键，不要覆盖
QQ 凭据、部门账号设置或其他现有内容；每个键只保留一条有效赋值：

```dotenv
# QQ
QQ_APP_ID=<当前部门QQ机器人AppID>
QQ_CLIENT_SECRET=<当前部门QQ机器人密钥>
QQ_ALLOWED_USERS=
QQ_GROUP_ALLOWED_USERS=*
QQ_ALLOW_ALL_USERS=true
QQBOT_GROUP_RECEIVE_MODE=all
QQBOT_GROUP_MESSAGE_CREATE_MODE=mention
QQBOT_GROUP_CONTEXT_MESSAGES=20
QQBOT_GROUP_CONTEXT_BUFFER_MESSAGES=100
QQBOT_GROUP_CONTEXT_CHARS=4000
QQBOT_GROUP_CONTEXT_SUMMARY_CHARS=1200

# QQ 长期消息快照
MESSAGE_SNAPSHOT_MEDIA_STORAGE=link
MESSAGE_SNAPSHOT_CONTEXT_MESSAGES=20
MESSAGE_SNAPSHOT_CONTEXT_TOKENS=4000
MESSAGE_SNAPSHOT_SEARCH_CANDIDATES=200

# Codex app-server 长周期任务（0 = 不设墙钟截止）
HERMES_CODEX_APP_SERVER_TURN_TIMEOUT_SECONDS=0

# Hermes session → Codex project/thread 持久映射
HERMES_CODEX_SESSION_PROJECTS_ENABLED=true
# 为安装 hotfix 前已存在的 channel session 自动补建项目（默认 true）
HERMES_CODEX_SESSION_PROJECTS_BACKFILL=true
# 将项目一次性注册到 Codex App 侧边栏（桌面账号部署时启用）
HERMES_CODEX_APP_REGISTER_PROJECTS=true
# 可选：Gateway 的 PATH 找不到 codex 时填写绝对路径
# HERMES_CODEX_APP_CLI=/绝对路径/codex
# 可选：仅这些平台用户可以通过提示词或命令切换项目
HERMES_CODEX_PROJECT_ADMIN_USERS=<QQ管理员openid，多个用逗号分隔>
# 可选：允许用户选择的项目别名和目录根
HERMES_CODEX_PROJECT_ALIASES={"finance":"/绝对路径/finance"}
HERMES_CODEX_PROJECT_ALLOWED_ROOTS=/绝对路径/部门项目根目录
```

仅对新建且不使用 WhatsApp 的 profile 加入：

```dotenv
WHATSAPP_ENABLED=false
```

已有 profile 必须保留 `WHATSAPP_ENABLED` 当前值，并与
`platforms.whatsapp.enabled` 一致；不要因 QQ/Codex 更新改写它。命名 profile 还要按
第 9、11 节设置自己的 `CODEX_HOME`。

保存后收紧权限：

```bash
chmod 600 "$DEFAULT_ENV"
```

说明：

- `QQ_GROUP_ALLOWED_USERS=*` 是 QQ 群聊启用的关键本地设置。
- `QQ_ALLOW_ALL_USERS=true` 与空的 `QQ_ALLOWED_USERS` 表示不再使用 QQ 私聊用户白名单；
  机器人可接收所有 QQ 用户的私聊消息。
- QQ 群主还必须在群机器人设置中开启“获取全部群消息”。未送达 Gateway 的消息无法
  被任何 hotfix 或数据库捕获。
- `QQBOT_GROUP_MESSAGE_CREATE_MODE=mention` 表示未 mention 消息只进入上下文和快照，
  不触发 Agent。
- WhatsApp 的 config 和 `.env` 必须表达同一状态；新建且关闭时两处都为 false，已有
  profile 则原样保留。只改一处可能被另一处反向覆盖。
- `MESSAGE_SNAPSHOT_MEDIA_STORAGE=link` 对 QQ 保存链接和元数据。
- Hermes 0.20.0 原生 Codex app-server 固定在 600 秒截止；上面的变量由
  `codex-app-server-phase-hotfix` 1.8.3 读取。多个聊天各自持有独立 Codex session，
  不共享 deadline 或 final 状态；同一聊天仍服从 `display.busy_input_mode`。
- 1.8.3 默认在 `$HERMES_HOME/codex-projects/<session_key>` 创建同名 Codex 项目；
  同一 `session_key` 后续经 `/new`、`/reset` 产生的新 `session_id` 会在该项目中创建
  同名 thread，Gateway 重启或缓存淘汰则恢复原 thread。1.6.x 的首次 session ID
  目录会在首次访问时自动迁移。
- `HERMES_CODEX_SESSION_PROJECTS_BACKFILL=true` 会在插件加载时读取 Hermes 自身的
  `sessions.json`，为安装前已有的 QQ 等 channel route 补建项目和映射；该
  route 下一条真实消息再按当前 session ID 创建 thread，不猜测或搬运归属不明的旧 thread。
- `HERMES_CODEX_APP_REGISTER_PROJECTS=true` 会在首个 Codex turn 后异步调用官方跨平台
  入口 `codex app <项目目录>`，使目录进入 Codex App 侧边栏；成功后写入映射数据库，
  避免每轮重复拉起 App，且 Desktop 启动不会阻塞消息回复。macOS 后台 Gateway 会通过
  `launchctl asuser` 进入当前登录账号的 Aqua 会话；Linux/Windows 则直接调用 CLI。
  不要在无桌面会话或容器内启用；容器部署应由宿主机同一桌面账号
  对宿主机映射目录执行 `codex app <路径>`。Windows 目录名会把非法的半角 `:` 替换为
  全角 `：`，数据库中的 `project_name` 仍为原始完整 session key。
- `HERMES_CODEX_PROJECT_ADMIN_USERS`、别名和允许根只在需要通过提示词切换项目时设置；
  不需要切换时可以省略。`QQ_ALLOW_ALL_USERS=true` 时不要把管理员列表设为 `*`。

## 7. 安装最新持久化插件

拉取 `hermes-dispatch` 最新 `main`。若已有仓库 `git status --short` 非空，先停止并处理
本机改动，不得 reset 或让 pull 覆盖：

```bash
mkdir -p "$HOME/src"
if [[ -d "$HOME/src/hermes-dispatch/.git" ]]; then
  test -z "$(git -C "$HOME/src/hermes-dispatch" status --porcelain)" || {
    echo "hermes-dispatch 工作树非干净，停止更新" >&2
    exit 1
  }
  git -C "$HOME/src/hermes-dispatch" switch main
  git -C "$HOME/src/hermes-dispatch" pull --ff-only origin main
else
  git clone --branch main --single-branch \
    https://github.com/mwe-support/hermes-dispatch.git \
    "$HOME/src/hermes-dispatch"
fi
cd "$HOME/src/hermes-dispatch"
```

将兼容层复制到持久化目录：

```bash
scripts/install-plugins.sh "$HOME/.hermes" \
  codex-app-server-phase-hotfix \
  qqbot-connect-hotfix \
  message-snapshot-store
```

安装命令必须显式列出插件名；省略插件列表会使用安装器默认集合并额外安装
`whatsapp-bridge-policy-hotfix`，不符合本手册范围。

更新已有插件时，安装器会先将当前目录完整备份到对应 profile 的
`plugin-backups/<插件>-<版本>-<时间戳>`；该目录位于 `plugins` 发现路径之外，
不会重复加载旧 `plugin.yaml`。记下输出的精确备份路径。若需回滚，例如：

```bash
scripts/install-plugins.sh --restore \
  "$HOME/.hermes" \
  qqbot-connect-hotfix \
  "$HOME/.hermes/plugin-backups/qqbot-connect-hotfix-<版本>-<时间戳>"
```

恢复命令会先备份当前活动版本，并拒绝使用位于 `plugins` 发现路径内的备份；
恢复后只重启目标 profile，并重新检查插件版本和 QQ `Ready`。
安装和恢复都会拒绝符号链接形式的 `plugin-backups` 或活动插件目录，并要求活动插件的
canonical 路径是 canonical `plugins` 根的直接子目录；`.` 和 `..` 不是合法插件名。
一次安装多个插件时，安装器会先完成全部活动目标的 canonical 预检，再开始创建、备份或
替换；后续任一目标不合法时，前面的插件保持原样且不会产生备份。
备份根验证先于缺失活动目录的创建，因此 fresh install 被拒绝时不会留下空插件目录。
出现任一拒绝时，不得手工绕过检查，应先修复 profile 的目录布局。

启用插件和消息检索工具集：

```bash
hermes -p default plugins enable openai-codex
hermes -p default plugins enable codex-app-server-phase-hotfix --no-allow-tool-override
hermes -p default plugins enable qqbot-connect-hotfix --no-allow-tool-override
hermes -p default plugins enable message-snapshot-store --no-allow-tool-override

hermes -p default tools enable --platform qqbot message_snapshot
hermes -p default tools enable --platform qqbot codex_session_project
hermes -p default tools list --platform qqbot
```

精确安装三项插件不会新增、升级、删除、启用或禁用 `whatsapp-bridge-policy-hotfix`。
更新已有 profile 时，记录并保留该插件原有的安装和启用状态。

三项插件共同提供：

- Codex app-server 阶段消息、长周期等待、媒体回传和审批兼容；
- Codex 项目按稳定 channel session 归档，thread 按 Hermes session ID 命名并可恢复；
- QQ 单次 final、群被动消息上下文、引用媒体和审批按钮兼容；
- QQ SQLite 长期快照、精确过滤、FTS5/BM25、模糊召回和恢复。

## 8. 运行插件回归测试

```bash
cd "$HOME/src/hermes-dispatch"
(
HERMES_PY="$HOME/.hermes/hermes-agent/venv/bin/python"
TEST_HERMES_HOME="$(mktemp -d /private/tmp/hermes-plugin-tests.XXXXXX)"
export HERMES_HOME="$TEST_HERMES_HOME"
export PYTHONPATH="$HOME/.hermes/hermes-agent"
export PYTHONDONTWRITEBYTECODE=1

"$HERMES_PY" plugins/codex-app-server-phase-hotfix/test_hotfix.py
"$HERMES_PY" plugins/qqbot-connect-hotfix/test_hotfix.py
"$HERMES_PY" plugins/qqbot-connect-hotfix/test_expired_reply.py
"$HERMES_PY" plugins/qqbot-connect-hotfix/test_media_reply.py
"$HERMES_PY" plugins/qqbot-connect-hotfix/test_group_roundtrip.py
"$HERMES_PY" plugins/qqbot-connect-hotfix/test_final_delivery.py
"$HERMES_PY" plugins/qqbot-connect-hotfix/test_streaming.py
"$HERMES_PY" plugins/qqbot-connect-hotfix/test_steer.py
"$HERMES_PY" plugins/message-snapshot-store/test_store.py
"$HERMES_PY" plugins/message-snapshot-store/test_capture.py
"$HERMES_PY" plugins/message-snapshot-store/test_materialize.py
"$HERMES_PY" plugins/message-snapshot-store/test_quoted_attachment.py
"$HERMES_PY" plugins/message-snapshot-store/test_whatsapp_capture.py
scripts/test_install_plugins.sh
git diff --check
)
```

`test_whatsapp_capture.py` 只验证已安装快照插件的兼容边界，不会启动 WhatsApp 或发送消息。
临时 `HERMES_HOME` 防止测试加载生产 profile 中已安装的旧插件或写入其数据库；不要删掉
这项隔离后直接运行 `test_steer.py`。
任一测试或 `git diff --check` 失败都先停止部署，不改写插件目录、不重启生产 Gateway。

## 9. 让 Codex app-server 接收 Hermes MCP

仅设置 `model.openai_runtime=codex_app_server` 不会自动保证 Codex 已注册 Hermes MCP。
默认 Hermes profile 复用默认 `CODEX_HOME=~/.codex`：

```bash
env -u CODEX_HOME hermes -p default --cli
```

在 Hermes CLI 中输入：

```text
/codex-runtime on
/exit
```

验证 Codex 侧注册结果：

```bash
env -u CODEX_HOME codex mcp get hermes-tools --json
env -u CODEX_HOME codex mcp list
```

必须能看到 `hermes-tools`。然后新建一次 Codex 会话，实际调用 Hermes 提供的工具验证，
不要只检查配置文件。

命名 profile 若存在用户相关 MCP，必须使用独立 `CODEX_HOME`，例如：

```bash
mkdir -p "$HOME/.codex-hermes/sales"
chmod 700 "$HOME/.codex-hermes/sales"
nano "$HOME/.codex-hermes/sales/config.toml"
```

为该目录写入第 3 节的权限策略，但不要复制默认 `config.toml` 中的 MCP 表。然后在
`$HOME/.hermes/profiles/sales/.env` 中只保留一条：

```dotenv
CODEX_HOME=/Users/<当前macOS账号>/.codex-hermes/sales
```

独立 `CODEX_HOME` 不会自动继承默认目录的 Keychain 登录。凭据必须选择以下一种方式，
并在继续 MCP 迁移前验证。

使用 `keyring` 时，为这个 `CODEX_HOME` 单独登录：

```bash
CODEX_HOME="$HOME/.codex-hermes/sales" codex login
CODEX_HOME="$HOME/.codex-hermes/sales" codex login status
```

若明确要求多个 profile 复用默认文件凭据，则将默认和命名目录的
`cli_auth_credentials_store` 都设为 `"file"`，先确认默认 `auth.json` 有效，再在命名目录
不存在任何 `auth.json` 的前提下创建链接：

```bash
env -u CODEX_HOME codex login
env -u CODEX_HOME codex login status
test -f "$HOME/.codex/auth.json"
test ! -e "$HOME/.codex-hermes/sales/auth.json"
ln -s "$HOME/.codex/auth.json" "$HOME/.codex-hermes/sales/auth.json"
CODEX_HOME="$HOME/.codex-hermes/sales" codex login status
```

两条路径的最后一条 `login status` 都必须显示已登录。不要覆盖命名目录中已有的凭据；
若状态仍是 `Not logged in`，先修复认证，不启动 Gateway。随后为每个命名 profile 分别
执行迁移：

```bash
CODEX_HOME="$HOME/.codex-hermes/sales" hermes -p sales --cli
```

在 CLI 中执行同样的 `/codex-runtime on` 和 `/exit`，再验证该目录：

```bash
CODEX_HOME="$HOME/.codex-hermes/sales" codex mcp get hermes-tools --json
CODEX_HOME="$HOME/.codex-hermes/sales" codex mcp list
```

`codex mcp list` 只能证明配置存在。若同名 MCP 在各 profile 使用不同用户认证，应在每个
`CODEX_HOME` 各建一个 Codex 会话，调用一个无写入副作用、能区分权限范围的真实工具；必须
只返回该 profile 用户可访问的数据。任何跨 profile 结果都视为隔离失败。

## 10. 启动 Gateway

安装并启动用户级服务：

```bash
env -u CODEX_HOME hermes -p default gateway install --force --no-start-now --start-on-login
env -u CODEX_HOME hermes -p default gateway start
env -u CODEX_HOME hermes -p default gateway status
env -u CODEX_HOME hermes -p default status
env -u CODEX_HOME hermes -p default logs -f
```

启动日志应显示：

- QQ adapter 已连接并到达 `Ready`；
- WhatsApp 关闭的 profile 没有 `Connecting to whatsapp` 或 reconnect 日志；启用的 profile
  则必须正常连接，不能把启动日志误判为故障；
- `message-snapshot-store` 已加载；
- Codex app-server 已启动且 `hermes-tools` 可用；
- 无重复 Bot 凭据、端口冲突或数据库权限错误。

## 11. 部门内创建多个小组 profile

Hermes profile 是同一 macOS 部门账号内的小组级隔离层。每个 profile 有独立的
`config.yaml`、`.env`、SOUL、会话、记忆、插件目录、日志和消息快照。默认 profile
使用 `~/.codex`；命名 profile 使用独立 `CODEX_HOME`，可按第 9 节有条件地复用同一
macOS 账号的 Codex 登录凭据，但不得共享 `config.toml` 或用户相关 MCP 配置。

从已配置的 default 克隆模板，不需要停止正在服务的默认 Gateway：

```bash
env -u CODEX_HOME hermes -p default profile create sales --clone --description "销售小组专用 Agent"
env -u CODEX_HOME hermes -p default profile create finance --clone --description "财务小组专用 Agent"
hermes -p default profile list
hermes -p default profile show sales
```

`--clone` 会复制配置和 `.env`，其中可能包含 default 的 Bot 凭据。**在启动任何小组
Gateway 前**，必须分别替换 QQ 凭据、允许列表和端口；不得让两个 Gateway 使用同一 Bot：

```bash
nano "$HOME/.hermes/profiles/sales/.env"
nano "$HOME/.hermes/profiles/finance/.env"
```

为同时运行的独立 Gateway 设置不同端口，例如：

```dotenv
# sales
API_SERVER_PORT=8643
CODEX_HOME=/Users/<当前macOS账号>/.codex-hermes/sales
```

```dotenv
# finance
API_SERVER_PORT=8644
CODEX_HOME=/Users/<当前macOS账号>/.codex-hermes/finance
```

按第 9 节创建两个 `CODEX_HOME`、设置权限/凭据方式，并分别完成 MCP 迁移与真实隔离测试。
Gateway 会在启动时从该 profile 的 `.env` 加载 `CODEX_HOME`；不要手改 LaunchAgent plist，
因为下一次 `gateway install --force` 会重新生成它。

把最新版兼容插件安装到每个 profile：

```bash
cd "$HOME/src/hermes-dispatch"
scripts/install-plugins.sh "$HOME/.hermes/profiles/sales" \
  codex-app-server-phase-hotfix \
  qqbot-connect-hotfix \
  message-snapshot-store
scripts/install-plugins.sh "$HOME/.hermes/profiles/finance" \
  codex-app-server-phase-hotfix \
  qqbot-connect-hotfix \
  message-snapshot-store
```

用 `-p` 对指定 profile 执行配置和插件命令：

```bash
hermes -p sales config set platforms.qqbot.enabled true
hermes -p sales config set streaming.enabled true
hermes -p sales config set streaming.transport auto
hermes -p sales config set agent.gateway_timeout 7200
hermes -p sales config set agent.gateway_timeout_warning 900
hermes -p sales config set agent.restart_drain_timeout 300
hermes -p sales plugins enable codex-app-server-phase-hotfix --no-allow-tool-override
hermes -p sales plugins enable qqbot-connect-hotfix --no-allow-tool-override
hermes -p sales plugins enable message-snapshot-store --no-allow-tool-override
hermes -p sales tools enable --platform qqbot message_snapshot
hermes -p sales tools enable --platform qqbot codex_session_project
hermes -p sales config check
```

仅当这个**新** profile 不使用 WhatsApp 时设置：

```bash
hermes -p sales config set platforms.whatsapp.enabled false
# 并确认 sales/.env 只有一条 WHATSAPP_ENABLED=false
```

如果是更新已存在的 profile，保留其 WhatsApp config 和 `.env` 当前状态。

为每个小组安装并启动独立 LaunchAgent：

```bash
hermes -p sales gateway install --force --no-start-now --start-on-login
hermes -p sales gateway start
hermes -p sales gateway status

hermes -p finance gateway install --force --no-start-now --start-on-login
hermes -p finance gateway start
hermes -p finance gateway status
```

日常管理命令：

```bash
hermes -p default profile list
hermes -p sales logs -f
hermes -p sales gateway restart
env -u CODEX_HOME hermes -p default gateway status
```

如果各小组不需要独立机器人连接，不要启动多套 Gateway；可只保留 default Gateway，
再单独设计 Hermes 的 multiplex/profile route。该路由依赖实际群 ID，不应直接照抄模板。

## 12. 最小验收

验收分层记录，不能用较低层结果代替真实 QQ 或完整运行验证。

### A. 配置与单测通过

对每个目标 profile 分别确认：

- 第 8 节全部测试和 `git diff --check` 通过；
- `config check` 通过，三项插件版本与仓库一致且已启用；WhatsApp hotfix 的安装及
  enabled/disabled 状态与更新前记录一致；
- `approvals.mode=smart`、三个 timeout、streaming 和 session-project 环境键符合预期；
- `.env` 中每个受管键只有一条有效赋值，QQ 凭据内容不打印到报告；
- 命名 profile 的 `CODEX_HOME` 不同，`codex mcp list` 与真实只读 MCP 调用都通过隔离测试。

默认 profile 使用 `env -u CODEX_HOME hermes -p default ...`；命名 profile 使用
`hermes -p <name> ...`，并以其 `.env` 中的 `CODEX_HOME` 执行 `codex mcp ...`。

### B. Gateway 与渠道就绪

只重启本次变更的目标 profile，确认：

- Gateway 为单实例且状态正常；
- QQ 完成 token refresh、WebSocket connected 和 `Ready`；
- WhatsApp 日志与该 profile 的预期状态一致；
- 没有端口冲突、数据库权限错误、插件重复加载或 Codex active writer 错误。

### C. 真实 QQ 与 Codex 项目通过

在 QQ 客户端按平台回复次数限制做最小实测：

1. 群聊先 mention 对应 Bot：只响应一次，必要中间进度可见，final 不重复；不 mention
   的消息不触发 Agent，但可被下一次 mention 从快照上下文引用。
2. 私聊执行短工具任务：中间状态在同一流式气泡更新，final 只返回一次。
3. 私聊中途 steer：旧气泡封口、出现 redirect/确认、新气泡继续；Codex 实际采用更正，
   thread 不变，final 只返回一次。
4. `/codex-project status` 的项目名等于完整 Hermes `session_key`，注册状态为
   `registered`；Codex App 侧边栏出现该项目。Gateway 重启后恢复同一 thread，`/new`
   后项目不变并创建以新 `session_id` 命名的 thread。
5. 如启用审批、媒体或项目别名，再分别验证发起人审批、引用附件和管理员权限边界。

### D. 完整运行验收（按发布风险执行）

- 30 分钟以上长任务无 600 秒 deadline，最终只回传一次；同时从另一 session 发起短任务，
  两者完成且不串线。
- 重启 Mac 并登录账号后，各 LaunchAgent 自动恢复且无重复实例或端口冲突。

小型文档或单插件补丁可不重复 D 层，但最终报告必须分别写明 A/B/C/D 的
`通过`、`失败` 或 `未执行`；只有实际完成的层级才能声明通过。

## 13. 更新与回滚

### 13.1 更新前检查

先记录版本、目标 profile、Gateway 状态和仓库改动。仓库非干净时停止，不 reset：

```bash
REPO="$HOME/src/hermes-dispatch"
git -C "$REPO" status --short --branch
test -z "$(git -C "$REPO" status --porcelain)" || {
  echo "工作树非干净，停止更新" >&2
  exit 1
}
git -C "$REPO" rev-parse --abbrev-ref HEAD
git -C "$REPO" rev-parse HEAD
env -u CODEX_HOME hermes -p default --version
codex --version
env -u CODEX_HOME hermes -p default gateway status
hermes -p sales gateway status  # 示例命名 profile
```

确认工作树干净后再快进到已审核的 `origin/main`：

```bash
git -C "$REPO" switch main
git -C "$REPO" fetch origin main
git -C "$REPO" merge --ff-only origin/main
git -C "$REPO" diff --check
git -C "$REPO" rev-parse HEAD
```

先按第 8 节运行全部测试，再用临时目录验证安装器；任一失败都不得改写生产插件或重启：

```bash
VERIFY_HOME="$(mktemp -d /private/tmp/hermes-dispatch-verify.XXXXXX)"
"$REPO/scripts/install-plugins.sh" "$VERIFY_HOME" \
  codex-app-server-phase-hotfix \
  qqbot-connect-hotfix \
  message-snapshot-store
```

### 13.2 二进制更新与插件更新分开

只有明确需要升级 Codex 或 Hermes 时才执行二进制更新；单独发布 hotfix 时跳过本段，避免
扩大变量。Hermes 更新会影响共享安装，先规划所有 profile 的重启窗口：

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
hermes -p default update --check
if hermes -p default update --help | rg -q -- '--plan'; then
  hermes -p default update --plan
fi
hermes -p default update --backup
hermes -p default --version
codex --version
```

测试通过后才将所需插件显式安装到每个目标 profile。只更新 QQ hotfix 时只列该插件；
更新完整三项兼容层时使用：

```bash
cd "$REPO"
scripts/install-plugins.sh "$HOME/.hermes" \
  codex-app-server-phase-hotfix \
  qqbot-connect-hotfix \
  message-snapshot-store
scripts/install-plugins.sh "$HOME/.hermes/profiles/sales" \
  codex-app-server-phase-hotfix \
  qqbot-connect-hotfix \
  message-snapshot-store
```

安装器输出的每个外部备份路径都要记录。再次运行第 8 节测试和目标 profile 的
`config check`；仍全部通过后，只重启目标 Gateway：

```bash
hermes -p sales gateway restart
hermes -p sales gateway status
hermes -p sales logs -f
```

不要用默认 `hermes gateway restart` 代替命名 profile 命令。按第 12 节分层验收，并记录：

```bash
git -C "$REPO" rev-parse HEAD
awk '$1 == "version:" {print FILENAME, $2}' \
  "$HOME/.hermes/profiles/sales/plugins/"*/plugin.yaml
```

### 13.3 回滚

只回滚 Codex 长任务机制：

```bash
hermes -p sales config unset agent.gateway_timeout
hermes -p sales config unset agent.gateway_timeout_warning
hermes -p sales config unset agent.restart_drain_timeout
nano "$HOME/.hermes/profiles/sales/.env"  # 将 HERMES_CODEX_APP_SERVER_TURN_TIMEOUT_SECONDS 改为 600
hermes -p sales gateway restart
```

这样只恢复 Codex 600 秒墙钟和该 profile 的 Hermes 默认 Gateway 超时，不移除阶段消息、图片、审批
与 session-project 能力。

只停止新的 session-project 映射和 Desktop 注册：

```bash
nano "$HOME/.hermes/profiles/sales/.env"
# 设置 HERMES_CODEX_SESSION_PROJECTS_ENABLED=false
# 设置 HERMES_CODEX_APP_REGISTER_PROJECTS=false
hermes -p sales gateway restart
```

禁用插件不会删除 `$HERMES_HOME/state/codex-session-projects.sqlite3`、
`$HERMES_HOME/codex-projects` 或 `PROJECT_MEMORY.md`；重新启用后可以继续恢复映射。

优先用第 7 节安装器输出的精确备份目录恢复单个插件。若只需停用兼容层而保留消息数据：

```bash
hermes -p sales plugins disable codex-app-server-phase-hotfix
hermes -p sales plugins disable qqbot-connect-hotfix
hermes -p sales plugins disable message-snapshot-store
hermes -p sales gateway restart
```

禁用 `message-snapshot-store` 不会删除数据库。只有明确决定销毁历史快照时，才单独删除
`$HERMES_HOME/message-snapshots`。
