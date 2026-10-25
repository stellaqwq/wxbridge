# wxbridge

从零实现的 Windows 微信 Python 开发库，不依赖、不导入 wxauto / wxauto4 / wxautox4。
聚焦单个可见会话的文字、文件、图片收发、语音转文字，以及可替换模型的 AI 自动回复。

现已加入 **OneBot v11 私聊与群聊适配器**：支持一个可见会话的文字和单图收发、文件下载上报和语音转文字接收，提供 HTTP API、HTTP 事件上报、正向和反向 WebSocket，可作为 AstrBot 等 AI 机器人框架的微信接入层，兼容其 Base64 表情图片发送。双击 `start-onebot.cmd` 自动打开 Web 控制台，在页面选择私聊或群聊、填写名称并连接，还可查看消息和媒体处理状态。群成员使用头像生成本地别名，不提供真实 wxid、完整名册或原生 @ 成员发送；配置、接口和边界见 [ONEBOT.md](ONEBOT.md)。无微信体验使用 `start-onebot.cmd -Mock`。

**当前是 0.2.0 实验版。已在本机微信 4.1.15.56 完成文字 AI 真人往返、文件/PNG 发送和保存回读、语音转文字验证。文件和 PNG 保存后 SHA256 与源文件一致；语音转写由用户确认正确。语音触发真实 AI 回复及长时间运行尚未完成验收。**

## 版本与实际边界

| 能力 | 状态 |
| --- | --- |
| 微信 4.1.15.56 无障碍控件激活 | 实机通过，163/166 个控件节点 |
| 当前会话标题与输入框独立核对 | 实机通过，包含前台语音占位提示 |
| 读取当前已加载消息正文 | 实机通过，正文来自 UIA，无 OCR |
| 发送文字并确认新消息出现在窗口 | 文件传输助手实机通过 |
| 启动基线、相同正文去重、窗口重建保护 | 自动化测试通过 |
| 模型回调、持久化处理记录、未知发送不重试 | 自动化测试通过 |
| 测试好友文字来源识别（当前浅色主题） | 已只读核对测试文字 |
| 真实好友完整 AI 往返 | 单条实机通过，对方确认收到；本地 Qwen3.5-9b |
| 文件发送、可见附件另存为 | 文件传输助手实机通过，合成文本文件 SHA256 一致 |
| 图片发送、可见图片另存为 | PNG 实机通过，单次样本 SHA256 一致；其他格式未验收 |
| 语音转文字 | 微信原生转文字实机通过，2 秒样本，用户确认正确 |
| 语音触发 AI 文字回复 | 显式开启，自动化测试通过；未做真人发送验收 |
| 群聊 | OneBot 支持一个可见群；成员为本地头像别名，详见 ONEBOT.md |
| 多窗口、全量历史、后台监听、原生语音发送 | 本版未实现 |

“支持最新版”需要以具体 DLL 和实机结果为准。当前仅允许修改已验证 SHA256 的微信 DLL 对应的进程状态；后续升级或同版本重新打包都会被拒绝，需要重新适配。

运行需要 Windows 10/11、64 位 Python 3.11～3.13、已登录且展开的微信，以及 Enter 发送设置。只读监听允许微信不获得焦点，但消息区域须完整可见、无遮挡，可与浏览器并排；发送、切换会话和附件操作仍要求微信位于前台。当前只验收 Python 3.12；其他 Python 版本未实测。

## 安装和诊断

```powershell
cd D:\ai\Codex\wxbridge
uv sync --locked
uv run wxbridge doctor
```

`doctor` 默认只读，不启用无障碍开关。

用户明确接受进程内无障碍开关后，可运行：

```powershell
uv run wxbridge doctor --enable-accessibility
uv run wxbridge messages --enable-accessibility --foreground
```

`--foreground` 显式显示并激活微信窗口，Windows 阻止直接激活时会尝试一次 Alt 焦点切换；仍失败则要求手动激活。若微信已关闭到托盘且内部聊天控件未显示，请从托盘手动打开。诊断启用的无障碍状态会在命令退出时恢复。

## Python 接口

先手动打开文件传输助手并让微信保持前台：

```python
from wxbridge import WeChat

with WeChat(enable_accessibility=True, foreground=True) as wx:
    print(wx.ChatInfo())
    wx.ChatWith("文件传输助手", exact=True)
    for message in wx.GetAllMessage():
        print(message.attr, message.type, message.content)
    result = wx.SendMsg("来自 wxbridge 的文字", who="文件传输助手")
    print(result.status)  # confirmed_in_ui，不代表服务器送达或已读
```

`ChatWith` 仅支持当前会话或已显示在左侧会话列表中的精确名称。名称必须唯一，本版没有全局联系人搜索。发送时 `who` 与当前聊天不一致会报错，不会自动切到其他联系人。

文字发送不会清除或拼接现有草稿。程序依次核对标题、草稿、正文回读和输入框焦点，再提交一次 Enter；随后仅接受新出现且正文完全一致的自己消息作为界面确认。没有读到确认会抛出 `DeliveryUncertain`，调用方不得自动重发。

```python
wx.GetNextNewMessage("唯一的测试好友备注")  # 首次调用仅建立基线
for message in wx.watch("唯一的测试好友备注"):
    print(message.sender, message.content)
```

切换会话、控件重建、完全丢失连续消息时会重建基线。重建期间的消息可能跳过，不追补回复；手动滚动和客户端消息虚拟化也可能导致漏读。UI ID 不是服务器消息 ID，不承诺完整历史或严格的端到端 exactly-once。

## 文件、图片和语音

```python
from wxbridge import WeChat

with WeChat(enable_accessibility=True, foreground=True) as wx:
    wx.ChatWith("文件传输助手")
    wx.SendFile(r"D:\test\report.pdf", who="文件传输助手")
    wx.SendImage(r"D:\test\photo.png", who="文件传输助手")
    for message in wx.GetAllMessage():
        if message.type in {"file", "image"} and message.attr != "other":
            path = wx.DownloadAttachment(message, r"D:\test\received")
            print(path)
        elif message.type == "voice" and message.attr != "other":
            print(wx.TranscribeVoice(message))
```

附件通过微信的系统文件选择框加入草稿。程序回读完整路径、检查源文件大小与修改时间，并在提交前核对草稿占位符、预览像素、聊天标题和输入框焦点。现有文字或附件草稿都会阻止发送。一次只提交一个附件，未知结果不重试。

`SendFile` 和 `SendImage` 返回 `MediaSendResult`。文件确认核对新自己消息的文件名，`verification="filename_and_side"`；图片确认只核对新出现的自己图片消息，`verification="new_image_and_side"`，UIA 不提供图片内容哈希。PNG 样本已另存并核对哈希，但不能据此保证所有图片均以原图传输。图片后缀候选为 PNG/JPG/JPEG/BMP/GIF/WebP，当前仅 PNG 完成实测。

`SendFiles([path1, path2], who=...)` 先检查全部路径，再逐个提交。失败抛出 `BatchSendError`，其 `completed` 保留已确认结果，`failed_path` 指向中断附件；不要重发整个列表。

接收接口识别当前已加载的 `file`、`image`、`voice` 消息。`DownloadAttachment` 通过微信的“另存为”保存可见文件/图片，生成唯一文件名，不覆盖已有文件，也不打开或执行附件。不可见、遮挡、已过期或菜单不提供另存为的附件会报错。它不提供后台全量下载。

`TranscribeVoice` 使用微信自带“转文字”，读取完整结果，不录制麦克风、不自动播放、不导出原始语音。语音时长、转换状态不能作为正文；转写前后同一条语音保持相同 UI 消息 ID。没有完整结果时会报错，不生成兜底回复。准确率和可用性仍取决于微信转写服务。

持续监听媒体可使用 `wx.watch(chat, message_types=("text", "file", "image", "voice"))`。图片/文件通过头像栏位置辅助识别来源，不使用图片内部颜色推断双方；默认浅色主题、窗口展开和无遮挡要求仍适用。保存、语音转写等操作仍需要前台，且会暂时占用 UI 工作线程，期间可能漏读其他到达的消息。

示例：`examples/media.py`；命令行发送：

```powershell
uv run wxbridge send-file 'D:\test\report.pdf' --to '文件传输助手' --enable-accessibility --foreground
uv run wxbridge send-image 'D:\test\photo.png' --to '文件传输助手' --enable-accessibility --foreground
```

## AI 自动回复

`AutoReply` 接受任意 Python 模型回调。后台轮询、模型处理和 UI/COM 执行分开；处理记录保存在 SQLite 中。默认只处理新到达的对方文字，启动前的消息、自己的消息、系统消息和未知来源消息均跳过。语音需要显式设置 `message_types=("text", "voice")`，并在回调中调用 `wx.TranscribeVoice(message)`。

```python
from wxbridge import WeChat
from wxbridge.autoreply import AutoReply

def reply(message):
    # 在这里调用你的模型，返回完整文本。
    return "AI 回复：" + message.content

with WeChat(enable_accessibility=True, foreground=True) as wx:
    worker = AutoReply(wx, "测试好友的唯一备注", reply)
    worker.run()  # Ctrl+C 停止
```

**来源判断仍是本版限制：微信只向 UIA 暴露正文，没有直接暴露双方标记。当前通过默认浅色主题的左侧白色/浅灰气泡和右侧绿色气泡辅助区分双方，媒体结合头像方向判断；深色主题、自定义背景、遮挡、部分裁剪或无法确认来源的消息会保留为 `other` 并跳过。已在测试私聊中完成一条真实 AI 往返；群聊使用 OneBot 接入，群成员身份为本地头像近似映射。** 自带 AI 示例仍用于私聊。

兼容聊天补全接口的模型调用示例位于 `examples/ai_reply.py`。该脚本默认只回复以 `/ai ` 开头的文字；需要显式指定联系人，没有默认真实发送对象：

```powershell
$env:AI_BASE_URL='http://127.0.0.1:1234/v1'
$env:AI_MODEL='你的已加载模型名'
# 远程模型另设 AI_API_KEY
uv run python examples/ai_reply.py --chat '测试好友唯一备注' --enable-accessibility --foreground
```

加上 `--voice` 后，指定私聊的新语音无需 `/ai ` 前缀，会先转文字，再将转写发往模型并回复文字；普通文字仍须测试前缀。图片和文件不会自动发往模型。默认不开启语音，开启前先在配合测试的私聊中验证。

消息将发往你配置的模型地址。示例不带历史记忆，不自动读取其他项目的密钥或配置。已有 `wechat-ai-bot` 项目未被改动或自动切换到此库。

## 实现与无障碍状态

公开技术资料说明了通过 Qt 无障碍状态恢复控件树的可行性。本库独立实现 PE 解析、附近指令验证、Win32 内存访问、COM 调用和消息处理，没有复制或包装其他微信库。

启用需要显式传入 `enable_accessibility=True`。流程为：只读扫描 Qt 无障碍代码，确认唯一 getter/setter 指向同一个布尔状态，核对 DLL SHA256、模块大小和进程中的指令，然后只修改该状态字节。未通过校验不写入；控件未恢复时回滚；正常关闭前再次核对指令后恢复原值。若模块已改变则跳过恢复写入。不会修改磁盘 DLL 或持久化系统无障碍设置。异常终止可能使该字节保持启用，直到微信进程退出。

直接通过 Windows UI Automation COM 接口定位控件，文字通过 Value Pattern 写入，核对焦点后用公开 SendInput 提交。每个客户端独占一个 COM 工作线程，并使用系统互斥锁避免多个 wxbridge 实例同时控制同一进程。它仍会受到锁屏、用户桌面操作和客户端更新的影响。

相关公开资料：[Microsoft UI Automation](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-uiautomationoverview)、[Qt QAccessible](https://doc.qt.io/qt-5/qaccessible.html)、[无障碍状态技术线索](https://github.com/fanyuantaier/wechatauto-replica/issues/1)。

## 开发验证

```powershell
uv sync --locked --group dev
uv run pytest -q
uv run ruff check src tests examples
```

测试使用模拟 UI/模型和合成 PE，不向真实联系人发送消息。`tools/probe_live.py`、`tools/probe_patterns.py` 是本机诊断脚本；`tools/smoke_live.py --send-self` 只向文件传输助手发送自检文字；`tools/media_acceptance.py --send-self --download` 只发送合成文件/PNG 并保存回读；`tools/voice_acceptance.py` 只在文件传输助手验证转写，不调用 AI、不发送回复。诊断树、截图、收发文件及原始转写可能含私人信息，保存在已忽略的 `data/` 下；安装包不包含它们。
