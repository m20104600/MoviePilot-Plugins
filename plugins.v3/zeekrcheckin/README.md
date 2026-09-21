# 极氪签到（ZeekrCheckin）

MoviePilot V3 插件：每天自动完成极氪 App 的
**签到 → 上报步数 → 做任务（阅读文章 / 每周点赞）→ 领取（碎片 / 能量球 / 极值）**。

逻辑移植自 [m20104600/zeekr-checkin](https://github.com/m20104600/zeekr-checkin) 的多客户端脚本，
行为一致；**不含 Token 抓取** —— Token 由用户在手机上抓一次后填进插件设置。

## 文件

- `__init__.py`：插件主类 `ZeekrCheckin`（单文件，零第三方依赖：`urllib` + 标准库）。
- 运行数据（最近一次结果）走宿主 `save_data()`，不写插件源码目录。

## 主要接口

| 方法 | 说明 |
|---|---|
| `init_plugin(config)` | 读配置（Token / 两组 cron / 步数 / 轮询开关…），可重复调用 |
| `get_form()` | 配置页：Token、签到定时、补领定时、步数、App 版本、轮询、通知、**「立即运行一次」开关** |
| `init_plugin()` | 保存配置时若 `run_now` 为真 → 复位开关（持久化，避免重载重跑）并起后台线程跑一次 |
| `get_page()` | 详情页：账号与 Token 有效期 + 最近一次运行结果 |
| `get_service()` | 按配置注册定时服务（`ZeekrCheckin.all.N` / `ZeekrCheckin.claim.N`） |
| `get_api()` | `GET /api/v1/plugin/ZeekrCheckin/run`（apikey，异步触发一次） |
| `run_checkin(mode, tag)` | 实际执行：`all` / `sign` / `claim` |
| `stop_service()` | 停用：打断进行中的领取循环并释放锁 |

## 定时

配置里两组多行 cron，每行「5 位 cron + 可选场次名」：

```
签到：1 0 * * * 凌晨场 / 10 8 * * * 早间场 / 30 21 * * * 晚间场
补领：10 0 * * * 凌晨补领 / 20 8 * * * 早间补领 / 40 21 * * * 晚间补领
```

奖励是异步入账的（1~3 分钟），所以默认主任务只领一轮 + 10 分钟后补领；
也可以开启「领取阶段轮询到领完」，让一次执行自己等到领完（约 3~5 分钟）。

## 实现要点

- 请求头签名：`x_ca_sign = SHA1(sort([SECRET, nonce, timestamp]).join(""))`，
  `x_ca_key: H5-SIGN-SECRET-KEY`，`app_code: toc_h5_green_zeekrapp`，`platform_h5: IOS`。
- 步数 secret：`"<步数>_salt"` 连续 base64 5 层。
- 账号 ID / 设备 ID 从 Token（JWT）里解析，不额外请求。
- 领取顺序固定：**能量球 → 极值 → 碎片**（能量球入账会让「减碳2000g」达标，进而生成新碎片）。
- 领取循环：0/45/105/180 秒检查点，连续 3 轮为空且距上次成功领取 ≥180 秒收工，硬上限 600 秒。
- 「步行3000步」未达标时补报一次步数再扫一圈（仅 `all` 模式）。
- **领取失败必须重试 + 如实上报**（2026-09-21）：
  - 批量 `batchApply` 里有个别 `success:false` → **真的**逐个重试（旧版只打日志，没重试），
    并把服务端返回的 `msg` 原样记进日志（旧版把原因丢掉了，事故因此查不出来）；
  - 只有**确实领到**的条目才记为已领，失败项留到下一轮再试（最多 3 次）；
  - 重试到上限仍有没领掉的 → 结论写「⚠️ 仍有 N 项未领到」，通知标题变
    「⚠️ 极氪签到（未领净）」，正文逐条列出 `名称→原因`，**不再**写「🎉 全部完成！」。

## 测试

```bash
python3 ../../tests/v3/zeekrcheckin/test_plugin.py      # 43 项单测（宿主为桩，网络为假响应）
python3 ../../tools/parity_check.py                    # 与手机端脚本逐字节对照编码/签名
python3 ../../tools/smoke_live.py --mode claim         # 真接口冒烟（幂等）
```
