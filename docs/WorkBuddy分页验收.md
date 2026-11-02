# WorkBuddy 分页验收

日期：2026-10-31。

## 已验证

- 加入 Codex → Claude → WorkBuddy 三页。保留原布局、时钟、最近三个项目、手动停留 60 秒和自动跟随逻辑。
- WorkBuddy 展示积分剩余比例和剩余积分，不套用 Codex / Claude 的 5 小时、周额度口径。真机本次读取为 75%、6,834.66 / 9,055 积分，数值随后会随使用变化。
- 任务从 `~/.workbuddy/workbuddy.db` 只读获取，按最近活动取三个不同项目；排除已删除、归档任务。
- 账号积分通过当前安装客户端使用的 `https://www.workbuddy.cn/billing/meter/get-user-resource-summary` 获取，每分钟更新。当前接口需要明确的 User-Agent；Python 默认请求头被拒绝，使用本服务的 `LX04-Status/1.0` 已成功读取。
- 登录凭证仅用于内存中的请求，不写日志、不写缓存、不修改账号文件；失败显示「—」。WorkBuddy 独立采集线程，不阻塞另两页。
- 绿色植物背景由内置 ImageGen 生成，页面使用优化后的 [plants.jpg](../display/plants.jpg)。原始生成图保留在本地。
- Chromium 61 真机已显示完整第三页，无裁切；已实际完成 Claude → WorkBuddy → Claude → WorkBuddy 的左右滑动。保持原有 viewport；隐藏页暂停动画，切到 WorkBuddy 时释放另两页的视频资源。[真机截图](images/page-workbuddy.png)
- 浏览器检查通过：首尾边界、邻页切换、滑动翻页、短滑回弹、取消滑动、独立指示点、手动停留、到期自动跟随、额度缺失时清空。
- Python 检查通过：积分合计、零余额、缺失字段、无限额度分支、项目去重、已删除任务过滤、状态映射、采集失败清空旧额度。Python / JavaScript 语法检查及本次修改文件的空白检查通过。

预览：`http://127.0.0.1:8477/?page=workbuddy`。手动选择后按原规则停留 60 秒，再自动跟随活跃任务。

## 待验证

- 企业账号的真实积分接口与无限额度套餐；目前只有解析分支验证。
- WorkBuddy 实际正在运行、等待授权的实时状态；本次真实任务样本均已完成，其他状态只做模拟数据验证。未知状态显示 Idle，不猜成正在运行。
- WorkBuddy 接口或本机数据格式升级后的兼容性。读取失败保留页面并显示「—」。

## 不做

不增加 WorkBuddy 语音控制、播报或写入能力，不修改 WorkBuddy 本身、系统配置和设备固件。

