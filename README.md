# MoviePilot 插件仓库 · 极氪签到

MoviePilot **V3** 插件：极氪 App 每日自动签到 / 上报步数 / 阅读文章 / 每周点赞 / 领取碎片·能量球·极值。

逻辑与手机端脚本 [m20104600/zeekr-checkin](https://github.com/m20104600/zeekr-checkin)
（QX / Loon / Stash / Egern / 青龙 那一套）**同源**，行为一致；
**唯一区别是不含 Token 抓取** —— Token 由你在手机上抓一次，粘进插件设置即可，
插件只读配置里的 Token，不模拟登录、不请求换取凭证。

```
plugins.v3/zeekrcheckin/__init__.py   插件主类 ZeekrCheckin（单文件，零第三方依赖）
package.v3.json                        插件市场索引
tests/v3/zeekrcheckin/test_plugin.py   31 项单测（宿主以桩替代，不需要 MP 环境）
tools/                                 密钥注入 / 端口一致性对照 / 真接口冒烟 / 图标 / 版本门禁
```

---

## 安装

**方式 A（插件市场，推荐）**：MoviePilot → 设置 → 插件市场 → 添加插件仓库地址：

```
https://github.com/m20104600/MoviePilot-Plugins
```

> 若你换到别的仓库名，请同步改两处：`package.v3.json` 里的 `icon`、
> `plugins.v3/zeekrcheckin/__init__.py` 顶部的 `ICON_URL`。

**方式 B（手动）**：把 `plugins.v3/zeekrcheckin/` 整个目录拷到 MoviePilot 的
`config/plugins/` 下，重载插件。

安装后在插件页「极氪签到」里填配置、点启用。

---

## 配置

| 字段 | 默认 | 说明 |
|---|---|---|
| 启用插件 | 关 | 关掉后不注册任何定时任务 |
| 立即运行一次 | 关 | 打开并保存后**马上按全流程跑一次**，跑完开关自动关掉（后台线程执行，不阻塞宿主；若上一次还在跑则本次跳过） |
| 发送通知 | 开 | 走 MoviePilot 的通知渠道（Telegram / 微信 / 邮箱等，按你 MP 的通知设置） |
| 输出明细日志 | 关 | 开启后日志里带逐轮明细（排查用） |
| 极氪 Token | 空 | `Bearer eyJ...`；**多个账号换行分隔**；兼容整段 `{"authorization":"..."}` |
| 签到定时 | `1 0 * * * 凌晨场` 等三行 | 每行一条：**5 位 cron + 可选场次名**（场次名只影响通知标题） |
| 补领定时 | `10 0 * * *` 等三行 | 同上，模式固定为「只领取」；留空 = 不补领 |
| 上报步数 | `10000` | 或 `off` 跳过上报 |
| App 版本 | 空 | 留空自动查 iTunes（失败退回 4.9.33） |
| 领取阶段轮询到领完 | 关 | 开 = 脚本内部轮询到领完（约 3~5 分钟）；关 = 只领一轮，靠「补领」定时兜底 |
| 强制点一次赞 | 关 | 排查用，忽略「本周已完成」 |

默认定时（与手机端 / systemd 三场一致）：

```
签到  1 0 * * *   凌晨场      补领  10 0 * * *   凌晨补领
签到  10 8 * * *  早间场      补领  20 8 * * *   早间补领
签到  30 21 * * * 晚间场      补领  40 21 * * *  晚间补领
```

想改成一天一场，就只留一行 `1 0 * * *`；想跑得更勤，加行即可（cron 用空格分段，场次名可省略）。

**为什么默认要「补领」**：极氪的碎片/能量球是任务做完后 **1~3 分钟**才入账的，
所以主任务跑完只领一轮，10 分钟后再跑一条只领取的任务收尾。开启「轮询到领完」也可以，
但那次执行会占住 3~5 分钟。

### Token 怎么来

1. 手机上照常配 Egern / QX / Loon / Stash 的抓取规则（或直接抓包，过滤域名
   `api-gw-toc.zeekrlife.com`，取请求头 `Authorization`）；
2. 打开极氪 App 点一下，通知里会有一整行 `Bearer eyJ...`；
3. 复制粘进插件的「极氪 Token」，保存启用。

Token 大约半年有效。插件详情页会显示账号与过期时间，剩不到 7 天会提示重新抓取。

### 手动跑一次

**配置页开关**：打开「立即运行一次」并保存 —— 立刻后台跑一次全流程，开关自动关回。
（已在跑时不重复触发，日志里会写「上一次还在跑」。）

**接口**：

```
curl "http://<你的MP地址>/api/v1/plugin/ZeekrCheckin/run?apikey=<你的API密钥>"
```

（默认 `all`；只领取可以带 `&mode=claim`。）接口是异步触发，立即返回，结果看通知与插件详情页。

---

## 与手机端脚本的差异

| 项 | 手机端脚本 | 本插件 |
|---|---|---|
| Token 获取 | 打开 App 自动抓取存入客户端 | **无**，用户手动填入 |
| 定时 | 客户端 cron / 青龙定时任务 | 插件配置里的两组 cron（可自定义多场） |
| 通知 | 客户端通知 / sendNotify | MoviePilot 通知渠道 |
| 多账号 | 青龙版支持 | Token 每行一个账号，逐个跑 |
| 领取轮询 | `POLL` / `WAITS` / `SETTLE` / `MAX` 参数 | 开关「轮询到领完」，内部仍用 45/60/75 秒与 180 秒观察窗、600 秒上限 |

---

## 本地开发与验证

不需要 MoviePilot 环境（宿主以桩替代）：

```bash
python3 -m compileall -q plugins.v3/zeekrcheckin     # 语法
python3 tests/v3/zeekrcheckin/test_plugin.py         # 31 项单测（网络全为假响应）
python3 tools/check_versions.py                      # 类/索引/history 版本一致
python3 tools/inject_secret.py                       # 填入签名 key（占位符 __ZEEKR_SECRET__）

# 需要本机有 zeekr-ports 时，可与手机端脚本逐字节对照编码/签名：
python3 tools/parity_check.py

# 真接口冒烟（用你自己的 Token；claim 幂等安全）：
python3 tools/smoke_live.py --mode claim
python3 tools/smoke_live.py --mode sign
python3 tools/smoke_live.py --run-now        # 走「立即运行一次」开关那条路（全流程）
```

在真实 MoviePilot 宿主里还应确认：插件市场能发现并安装、启用/停用/重载不残留后台任务、
`DEBUG=true` 时没有旧导入警告。CI（`.github/workflows/ci.yml`）跑语法 + 单测 + 版本门禁。

---

## 说明与免责

- 插件只使用你自己抓到的 `Authorization` Token，**不保存账号密码、不模拟登录**。
- 源码里的 `ZEEKR_SECRET` 是极氪 H5 前端的固定签名 key（公开前端里就有，非账号凭证），
  仓库里以占位符形式存在，由 `tools/inject_secret.py` 填入。
- 仅供个人学习与自用；上游接口调整可能导致失效，请自行评估风险。

## 更新记录

- **v1.1.0**：新增**「立即运行一次」开关** —— 配置页打开并保存后立刻按全流程跑一次，
  跑完开关自动关掉（后台线程执行不阻塞宿主；上一次还在跑则本次跳过）。
- **v1.0.0**：首次发布。移植 zeekr-checkin 的完整签到逻辑（签到 / 步数 / 阅读文章 /
  每周点赞 / 碎片·能量球·极值领取 + 延迟入账补领），自定义多场次 cron，
  多账号，Token 手动填入（不含抓取）。
