# DeepSeek 分页验收

日期：2026-10-31。

## 已验证

- 加入第四页，顺序为 Codex → Claude → WorkBuddy → DeepSeek，保留时钟、指示点、左右滑动、手动停留 60 秒及自动跟随。
- 接通 [DeepSeek 官方余额接口](https://api-docs.deepseek.com/api/get-user-balance/)，展示总余额与赠送余额。总余额已包含赠送余额，两者不能相加；不虚构 5 小时或周额度。当前读取为总余额 CNY 30.71、赠送余额 CNY 0.00。
- 只读取本机已有配置中启用的 `https://api.deepseek.com` 上游密钥，固定请求官方主机，不跟随重定向；密钥不写入页面、日志或新增文件，不修改配置。余额每分钟刷新，读取失败清空旧值。
- 本地任务只接入 `~/.codex/sessions` 中明确标记 `model_provider=deepseek` 的会话，按项目去重并取最近三个。原 Codex 页排除这类会话，避免重复归属。当前没有匹配的真实会话，页面显示「暂无 DeepSeek 任务」。
- 鲸鱼背景由内置 ImageGen 生成，页面使用 [whale.jpg](../display/whale.jpg)，800×480 JPEG。原图保留在本地。鲸鱼页使用静态完整构图，避免平移放大裁掉鲸鱼头部，也降低设备负担。
- 浏览器已验证四页边界、第三/四页往返、独立指示点、手动停留、到期跟随、金额与币种、空任务提示、余额缺失清空，以及切到第四页时释放前两页视频。
- LX04 真机已验证完整显示、WorkBuddy → DeepSeek → WorkBuddy → DeepSeek 来回滑动；保留原 viewport 和 Chromium 61 兼容写法。[真机截图](images/page-deepseek.png)
- 离线检查覆盖：币种不混加、零余额、无效响应、仅选择启用的官方密钥、任务归属、开始/完成状态、授权回复按 call ID 匹配、读取失败清空旧值、Codex 历史任务排除 DeepSeek。

预览：`http://127.0.0.1:8477/?page=deepseek`。

## 待验证

- 真实 DeepSeek 会话的运行、完成与等待确认状态。目前只有模拟日志验证，没有启动额外任务制造记录。
- 多币种真实账户；解析已按 CNY 优先、其次 USD 处理，不做汇率换算。
- DeepSeek 网页/App 聊天记录不在本次接入范围；当前任务来源是明确归属 DeepSeek 的本机 Codex 会话。

## 不做

不发起付费模型请求，不新增语音控制或交易能力，不修改密钥、系统配置或设备固件。

