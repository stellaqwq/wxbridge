# wxbridge 的 OneBot v11 适配器

底层使用本项目的 `WeChat`，向机器人框架提供 OneBot v11 通信。当前适配**一个可见私聊或群聊的文字、图片收发，以及文件和语音转文字接收**，并非完整 QQ 功能实现。AI 模型、提示词和对话记忆由上层机器人框架管理。

微信兼容性沿用 wxbridge 0.2.0：当前已验证 Windows 微信 **4.1.15.56** 的特定 DLL；窗口须展开，使用默认浅色主题和 Enter 发送。只读监听允许与浏览器并排、微信不获得焦点，但消息区须完整可见；发送和其他桌面操作仍要求微信获得焦点。不支持最小化监听或同时监听多个会话。

## 启动

在本目录双击 `start-onebot.cmd`。脚本安装锁定依赖，自动生成缺失的配置，并打开 **Web 配置页**。即使还没填写联系人，页面也能启动，无须手动编辑 JSON。

在网页中选择私聊或群聊，填写准确的联系人备注或群标题、机器人及目标映射 ID，按需设置反向 WebSocket 地址和无障碍兼容开关，然后点击 **保存并连接微信**。也可点击“仅保存”，稍后再连接。微信窗口或控件不可用时，错误显示在页面上，配置仍会保留；修正后可以直接重连。

Web 页可以复制 OneBot 访问令牌。原有令牌和其他设置不会因保存联系人而重置。管理页使用每次启动随机生成的独立管理口令；启动脚本打开的完整链接会自动解锁，刷新后保持当前管理会话。若直接打开 `http://127.0.0.1:6700/`，可从启动窗口复制完整的“Web 管理页面”链接。OneBot API 令牌不能用于更改管理配置。

以下是配置字段示意，供手动配置时参考：

```json
{
  "chat_name": "微信里该联系人的唯一备注",
  "self_id": 10000,
  "peer_id": 10001,
  "enable_accessibility": true,
  "reverse_ws_urls": ["ws://127.0.0.1:8080/onebot/v11/ws"]
}
```

保留生成文件中的其他字段和随机令牌。`enable_accessibility: true` 对应网页中的“无障碍兼容开关”，允许底座启用经校验的微信进程内无障碍状态，正常退出时恢复；具体行为见 README。默认值为 `false`，没有可用控件时会显示连接错误，Web 页继续运行。`foreground: true` 对应“启动时激活微信窗口”；只读监听可与浏览器并排，发送时仍需微信获得焦点。

点击连接前，先在微信里手动打开目标私聊或群聊。适配器不会根据消息里的数字 ID 自动寻找或切换会话。多个联系人或群使用同名标题无法可靠区分，先改成唯一名称。私聊与群聊类型也会校验，防止同名但类型不同的会话被误用。

## 群聊与 AstrBot

在 Web 选择 **群聊**，填写微信标题栏中的准确群名，设置本地群 ID（默认 `20001`），并填写机器人在群里的显示昵称。打开该群后保存并连接。配置对应 `chat_type="group" / chat_name / group_id / group_mention_name`。原有配置默认 `chat_type="friend"`，无需迁移。

群事件上报 `message_type="group"`、`sub_type="normal"`、`group_id`、成员 `user_id` 和 `sender`。可见文字中精确匹配 `@机器人昵称`（后接空白或结尾）时，转换为指向 `self_id` 的 `at` 段，供 AstrBot 的群唤醒逻辑使用。这里根据可见文字匹配，无法区分手工输入的同样文字与微信原生 @。没有填写昵称时保留普通文本；Web 始终显示原始正文。

在 AstrBot 启用该群的消息处理，并按需设置唤醒前缀或 @ 唤醒。关闭“回复时 @ 发送者”和引用回复：目前只支持纯文本或单张图片回复，不能按本地成员标识执行原生 @。`send_group_msg` 与带 `group_id` 的 `send_msg` 均可发送；填错群 ID 或调用私聊发送会拒绝，不切换微信会话。

**群成员身份是本地近似映射。** 微信 4.1.15.56 的群消息辅助功能控件未暴露成员昵称或 wxid；适配器仅从已确认收到的消息头像区域生成指纹，分配稳定的本地数字 ID 和“群成员-xxxxxx”别名。同头像成员可能合并，换头像可能生成新 ID，不适合权限判断。不会将私聊联系人 ID 套给所有群成员，也不会猜测群主或管理员身份。

`get_group_member_info/list` 只提供连接后监听到的成员和机器人自己，不是完整群名册，角色统一为 `member`。群人数读取标题栏独立人数控件；读取不到返回 `0`，最大人数未知也返回 `0`。支持当前一个可见群；没有群管理、入退群通知、完整历史、跨群发送或成员私聊能力。群标题与发送者标识已做实机只读核对；群收发协议和媒体使用隔离模拟测试，未向真实群发送测试消息。

命令行等价方式：

```powershell
uv sync --locked --extra onebot
uv run --extra onebot wxbridge onebot --open-browser
```

也可使用 `wxbridge-onebot --config 路径` 或 `python -m wxbridge.onebot.cli`，从终端打印的完整管理链接打开网页。`start-onebot.cmd -NoBrowser` 可禁止自动打开浏览器。`--init` 仅生成配置，不覆盖已有文件。数据库相对路径以**配置文件所在目录**为基准。配置项完整示例见 `onebot.example.json`。

## Web 消息监听

连接成功后点击左侧“消息监听”或页面顶部同名入口。页面每秒获取一次新记录，区分收到和发出的消息，显示发送者、时间及正文。收到图片和文件后，适配器通过微信“另存为”保存附件，并上报带下载链接的 OneBot 消息段；收到语音后，通过微信“转文字”取得正文，再作为文本消息上报。自己的媒体仅显示，不会触发模型。

媒体行显示“等待处理 / 正在处理 / 等待微信前台 / 已交给 OneBot / 上报失败”，同一消息更新状态而不重复增加行。“已交给 OneBot”表示进入事件上报队列，不是 AstrBot 或模型的回执。媒体失败会显示具体原因，不会用 `[图片]`、文件名或语音时长冒充正文。

保存附件和语音转文字需要微信位于前台，目标消息仍在可见列表。缺少焦点时每 5 秒重试，最多等待 5 分钟，不主动抢焦点；其他错误停止该条处理。单次 UI 保存或转写可能占用工作线程约 30 秒。媒体队列最多 32 条，单附件最多 100 MiB；已有语音转写结果可直接上报。原始语音不导出，也不会生成假的 `record` 链接。

“暂停显示”只暂停本页更新，OneBot 继续运行；“继续监听”读取暂停期间仍在缓冲区内的消息。“清空显示”仅清空本页，不删除数据库、不消费 OneBot 事件；同一标签页刷新后保留清空位置。最多显示最近 200 条，可关闭自动滚动。切换联系人或数字映射会隔离旧显示；服务重启会清空网页的内存缓冲。

首次连接仅建立基线，启动前的历史不会作为新消息显示。来源不明的条目不展示。文字记录来自界面和本地消息库，发送显示仅表示界面确认，不等于对方已读。

将微信与浏览器并排，确保微信消息列表完整露出。最小化、遮挡、锁屏、滚动历史、切换聊天或控件重建可能暂停读取并重建基线，期间可能漏报。页面显示连接状态和恢复提示。输入、发送和会话切换继续校验前台焦点；监听页面不会主动抢焦点或发送测试消息。

网页数据通过独立管理鉴权的 `GET /console/api/messages` 提供；支持 `session / after / limit` 游标，属于控制台接口，不改变 OneBot v11 协议。

## AstrBot 媒体接入

在 AstrBot 创建 OneBot v11 适配器，例如监听 `127.0.0.1:6701`，wxbridge 的反向地址填写 `ws://127.0.0.1:6701/ws`，两端访问令牌一致。接入后的行为如下：

| 微信消息 | OneBot 上报 | AstrBot 处理要求 |
| --- | --- | --- |
| 图片 | `image` 段，包含 `file` 和 HTTP `url` | 使用支持图片的模型，或配置图片描述模型 |
| 文件 | `file` 扩展段，包含 `file_id / file_name / file_size / url` | 使用支持附件的插件、文件解析或工具配置；收到文件不等于模型已读取其正文 |
| 语音 | 微信转写后的 `text` 段，附 `wxbridge.original_type=voice` | 按普通文本处理，无须配置语音识别服务 |

`file` 消息段是 AstrBot 支持的扩展，不是 OneBot v11 标准中的独立私聊文件段。适配 AstrBot 的实现参见其 [OneBot 适配器源码](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py)。本机安装版本也已核对相同字段。

附件存在消息库旁的 `<数据库名>-media` 目录，注册信息保存在相同 SQLite 消息库，重连后仍可查询。每个附件的下载链接使用独立随机令牌，不附带 OneBot 或 Web 管理口令；持有该链接即可下载对应附件，请勿公开。接口只提供已注册且仍在缓存中的文件，不接受任意本地路径。缓存暂不自动清理，停止服务后可按需清理旧附件，旧链接随之失效。

同机 AstrBot 的“媒体服务地址”留空即可，默认使用 `http://127.0.0.1:6700`。容器或远程 AstrBot 需要在网页填写它能访问的地址，例如 `http://192.168.1.10:6700`，同时在配置中将 `host` 设为所需监听网卡；只修改链接不会改变监听地址。无需关闭 OneBot 鉴权。媒体下载失败、图片模型不支持图片、文件解析未启用，均需查看 AstrBot 日志进一步区分。

### 表情图片发送

AstrBot 的 OneBot 适配器会将图片（包括 `meme_manager` 的 GIF 表情）转为 `base64://...`，其图片处理也可能把原图编码成 WebP。`send_private_msg` / `send_msg` 支持单个 `image` 段，通过底座 `SendImage` 发送；`can_send_image` 返回 `yes=true`。支持 PNG/JPEG/GIF/BMP/WebP，原始内容及转换后的单图均最多 8 MiB。也可使用本适配器已接收图片的 `file` 标识发送；不读取调用者指定的任意本地路径或网络 URL。

微信 4.1.15.56 的文件选择器会将 WebP 发成文件卡片。适配器因此会把静态 WebP 的实际内容转成 PNG，保留尺寸、颜色和透明度；动态 WebP 转成 GIF，保留帧时长和循环次数，再提交微信发送。不会仅改扩展名，也不会重发此前已发出的 WebP 文件。转换前限制单帧 1600 万像素、最多 200 帧、总计 3200 万像素，转换失败会在提交前返回错误。已注册的旧 WebP 图片标识同样经过转换。

GIF 在此版本微信中使用 `mmui::ChatBubbleReferItemView` 和固定标签“动画表情”，适配器按图片识别该组合并继续校验头像方向，避免动图已经发送却等待不到图片确认。其他引用消息不会因使用同一控件类而被当成图片。

示例：`{"action":"send_private_msg","params":{"user_id":10001,"message":[{"type":"image","data":{"file":"base64://图片编码"}}]},"echo":"meme-1"}`。图片必须单独调用；文字与图片混合、多图、闪照、语音发送仍会明确拒绝，整条请求不会部分发送。

HTTP API 和正、反向 WebSocket 接受最多 16 MiB 的请求，足够容纳上述图片的 Base64 编码；Web 配置请求仍限 64 KiB。旧版 64 KiB 的 WebSocket 限制会导致较大的表情帧断开，再由 AstrBot 报 `WebSocket API call timeout`。新版会对已接收请求的格式、图片大小、微信窗口和发送结果错误返回带原始 `echo` 的失败响应。超过 16 MiB 的 WebSocket 帧仍会被关闭；重连日志包含关闭码，便于定位。

发送时仍需目标微信会话位于前台且无草稿。私聊和群聊均支持单图，群聊使用 `send_group_msg` 或带 `group_id` 的 `send_msg`。界面确认后会持久化图片消息并显示在 Web；未取得确认返回 `1200 / retryable=false`，不能自动重发。

## ID 映射

OneBot v11 要求数字 ID，而此底座没有读取微信服务器 wxid。

| 字段 | 含义 |
| --- | --- |
| `self_id` | 给这个微信机器人分配的本地数字别名，默认 10000 |
| `peer_id` | 给 `chat_name` 对应联系人分配的本地数字别名，默认 10001 |
| `group_id` | 给当前群分配的本地数字别名，默认 20001，仅群聊模式使用 |
| 群成员 `user_id` | 根据头像指纹持久化分配的本地数字别名，可能因同头像合并或换头像改变 |
| `message_id` | SQLite 中递增的 int32 本地消息 ID，重启后可以继续查询 |

这些数字不是微信号、wxid 或 QQ 号。私聊发送的 `user_id` 必须等于 `peer_id`，群聊发送的 `group_id` 必须等于配置群 ID。`get_friend_list` 在私聊模式只返回这一条配置映射，群模式返回空数组。私聊数据库继续绑定 `self_id / peer_id / chat_name`；群数据库绑定 `self_id / group / group_id / chat_name`。通过网页改变类型或映射时，自动换用独立消息库，保留旧记录。手动编辑配置改变映射时仍需自行更换数据库路径。

## 连接方式

同一进程、同一端口同时提供 HTTP 和正向 WS，默认 `127.0.0.1:6700`。

| 方式 | 地址或配置 |
| --- | --- |
| HTTP API | `GET/POST http://127.0.0.1:6700/{action}`，支持尾斜杠 |
| 正向 WS 通用连接 | `ws://127.0.0.1:6700/`，另有 `/ws` 别名 |
| 正向 WS 仅 API / 仅事件 | `/api` / `/event`，支持尾斜杠 |
| 反向 WS | 设置 `reverse_ws_urls`；Universal 客户端，断线重连 |
| HTTP POST 上报 | 设置 `http_post_urls`，可用 `http_post_secret` 做 HMAC-SHA1 签名 |

HTTP / 正向 WS 带 `Authorization: Bearer 配置里的access_token`；也可用 `?access_token=...`。反向 WS 自动携带这个头，以及 `X-Self-ID`、`X-Client-Role: Universal`。HTTP POST 带 `X-Self-ID`，有 secret 时带 `X-Signature`。不要把令牌放进聊天消息。

### NoneBot 2 反向 WS

在已有的 NoneBot 项目安装并注册 `nonebot.adapters.onebot.v11.Adapter`，使用 FastAPI 驱动。在它的 `.env` 中设置：

```dotenv
DRIVER=~fastapi
HOST=127.0.0.1
PORT=8080
ONEBOT_ACCESS_TOKEN=填写wxbridge配置中的access_token
```

在 wxbridge 配置中设置 `reverse_ws_urls: ["ws://127.0.0.1:8080/onebot/v11/ws"]`，先启动 NoneBot，再启动 wxbridge。框架看到的机器人 ID 是 `self_id`，私聊用户 ID 是 `peer_id`。上层 AI 插件需支持 OneBot 私聊文本消息。

这些地址与鉴权方式按 [NoneBot 官方连接说明](https://onebot.adapters.nonebot.dev/docs/guide/setup/) 和 [OneBot v11 反向 WS 规范](https://github.com/botuniverse/onebot-11/blob/master/communication/ws-reverse.md) 实现。当前自动化验证使用模拟微信和协议客户端，未将真实微信接入第三方 AI 插件做真人验收。

### HTTP 调用

```powershell
$config = Get-Content -Raw -Encoding UTF8 .\data\onebot.json | ConvertFrom-Json
$headers = @{ Authorization = "Bearer $($config.access_token)" }
Invoke-RestMethod "http://127.0.0.1:6700/get_status" -Headers $headers
# 以下命令会向配置的真实微信联系人发送文字；模拟模式只写入内存。
$body = @{user_id=$config.peer_id; message="你好"; auto_escape=$true} | ConvertTo-Json
Invoke-RestMethod "http://127.0.0.1:6700/send_private_msg" -Method Post -Headers $headers `
  -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

HTTP POST 接受 JSON 对象或 urlencoded 表单，GET 接受 query 参数；另有 `POST /` 扩展，正文与 WS API 请求相同。API 请求和响应遵循 [OneBot v11 HTTP 规范](https://github.com/botuniverse/onebot-11/blob/master/communication/http.md)。

WS API 示例：

```json
{"action":"send_private_msg","params":{"user_id":10001,"message":"你好"},"echo":"request-1"}
```

响应的 `echo` 原样保留，可为任意 JSON 类型。连接后首先上报 lifecycle/connect，之后上报消息和心跳。仅 API 的 WS 连接不接收事件。事件消息使用数组格式；文字的 `raw_message` 做 CQ 文本转义，媒体使用相应 CQ 段。启动前历史、自己的消息、系统消息和来源不明的消息不产生好友消息事件。

HTTP POST 事件接收端可返回 `{"reply":"回复内容","auto_escape":true,"at_sender":false}`，回复到事件对应的私聊或群聊。仅处理回复正文及 `auto_escape`，不执行群成员 @、撤回、踢人或禁言等快速操作。空响应/204 表示不操作；元事件不触发回复。HTTP 快速操作处理完毕后再把同一事件推送给 WS。

## 支持范围和错误处理

| API | 当前行为 |
| --- | --- |
| `send_private_msg` / `send_group_msg` / `send_msg` | 发送到当前配置私聊或群聊；字符串、纯 text 数组、单个 text 对象，或单独的 image 段 |
| `get_msg` | 查询本适配器实际记录的收发消息，不读取微信全量历史 |
| `get_image` | 按已注册图片的 `file` 查询缓存路径、大小和下载链接 |
| `get_private_file_url` | 按已注册附件的 `file_id` 查询下载链接和文件名，AstrBot 兼容扩展 |
| `get_group_file_url` | 校验当前群 ID，再按已注册附件的 `file_id` 查询链接，AstrBot 兼容扩展 |
| `get_login_info` / `get_friend_list` / `get_stranger_info` | 返回配置的本地映射；无法查询其他微信联系人 |
| `get_group_list` / `get_group_info` | 群模式返回配置群及可读人数；私聊模式列表为空 |
| `get_group_member_info` / `get_group_member_list` | 当前群已观察到的本地成员映射和机器人自己，不是完整名册 |
| `get_status` | UI 可用性和运行状态；真实模式 `online=null`，无法据 UI 推断服务器在线状态 |
| `get_version_info` | `app_name=wxbridge`、`protocol_version=v11` 和支持的 actions |
| `can_send_image` / `can_send_record` | 图片 `yes=true`，语音发送 `yes=false` |
| `get_supported_actions` | 扩展接口，返回已实现的 actions |

支持上述接口的 `_async` 和 `_rate_limited` 后缀，返回 `status=async / retcode=1`。限速调用排队执行，`rate_limit_interval` 单位为秒。异步失败只写日志，不自动重试；最多排队 64 项。正常发送仍建议使用同步调用以取得明确结果。

`auto_escape=false` 时解析 CQ 文本转义；CQ 功能码字符串仍会报错，图片需使用结构化 `image` 段。`auto_escape=true` 或 `text` 数组可发送字面量 `[CQ:...]`。单条非空文本最多 4000 字符；混合不支持的消息段会整体拒绝，不部分发送、不截断或自动拆分。

底座的图片、文件、语音转文字功能用于上述媒体流程，也可直接通过 Python API 使用。OneBot 文件和语音发送、群发、群管理、加好友、撤回、引用和 QQ 专属接口未实现。

| 返回码 | 含义 |
| --- | --- |
| `0` / `1` | 成功 / 已接受异步任务 |
| `100` | 不支持的能力、未配置目标、窗口或草稿等操作阻断 |
| `1200` | 已到发送提交阶段，但结果不确定；`retryable=false`，必须人工核对 |
| `1400` / `1404` | 参数或正文错误 / 未实现 action |
| `1500` | 适配器内部错误 |

HTTP 缺少令牌返回 401、令牌错误 403、未知 API 404、不支持 Content-Type 406、超大请求 413。WS 鉴权失败拒绝握手。

发送成功只表示 `wxbridge` 在微信界面看到了新的相同自己消息，不表示服务器送达或对方已读。连接在发送中途断开也不能据此判定未发出，请勿自动重试。适配器不在外部网络失败时重发消息或补报事件；避免快速操作被重复执行。

启动、切换会话、控件重建或 UI 消息连续性丢失时，底座重新建立监听基线，期间可能漏报。WS 离线期间的事件不补发。事件主队列 256 项，满后丢弃新事件并将 `get_status.good` 标记为 false；WS 单连接队列 128 项，慢客户端会断开。长时间可靠性仍须实机验证。

## 无微信模拟与验证

```powershell
.\start-onebot.cmd -Mock
```

会生成 `data/onebot-demo.json` 并启动纯内存微信，不导入 Windows 后端、不读取微信、不向真实联系人发送。模拟模式的数据库自动加 `-mock` 后缀，与真实记录隔离。

使用模拟配置的令牌，向 `POST /wxbridge_mock_receive` 发送 `{"message":"模拟收到消息","sender":"模拟成员甲"}`，即可按配置产生私聊或群聊事件，让上层机器人回复。`sender` 可选，用于测试群成员区分；该扩展接口仅存在于 `--mock` 模式。

开发验证：

```powershell
uv sync --locked --extra onebot --group dev
uv run --extra onebot pytest -q
uv run --extra onebot python tools/onebot_smoke.py
uv run --extra onebot python tools/onebot_console_check.py
uv run ruff check src tests examples
```

测试涵盖 HTTP/WS 鉴权、消息转换、持久化 ID、目标校验、未知发送不重试、HTTP 签名与快速回复，以及真实回环 TCP 上的反向 WS 收发和重连。`onebot_smoke.py` 另启动真实 CLI 的 `--mock` 子进程，验证实际 HTTP/WS 端口；不操作微信。

`onebot_console_check.py` 使用独立临时配置、`--mock` 和无界面 Edge，实际填写联系人、保存并连接、刷新和断开，并检查桌面与手机布局；不读取日常浏览器资料、不操作微信。截图在 `data/onebot-checks/`。
