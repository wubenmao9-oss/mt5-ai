MT5-Ai 项目已知问题与修复指南
===============================

编写目标：将此文档交给新的AI智能体，它能据此理解和修复所有问题。
每个问题格式：严重程度 | 所属模块 | 问题描述 | 根因 | 修复方案

---

## 优先级: 🔴 致命 / 🟠 严重 / 🟡 中等 / 🔵 轻微

---

### 一、架构问题

#### 1. 🔴 引擎与Web服务器跑在独立进程，无共享内存
- **模块**: run.py / main.py / web.py
- **问题**: `run.py` 用 subprocess.Popen 分别启动 main.py (引擎) 和 web.py (FastAPI)。两个进程无法共享 Python 对象。
- **根因**: 之前用 `from src.web.server import app_state` 想跨进程传对象，这在 Python 里不成立——import 只加载模块，不共享内存。
- **现状**: 用了 `data/engine_state.json` 做 IPC 桥，引擎每30秒写入，Web API 每次请求读取。但仍有竞态条件（引擎写入中、Web正在读）。
- **修复建议**: 
  方案A: 改用 SQLite 做状态存储，引擎写入 trades/decisions 表，Web 读同一个库。
  方案B: 用 Redis 做 IPC。
  方案C: 把引擎内置到 FastAPI 里（asyncio.create_task 后台跑），不再拆两进程。

#### 2. 🟠 run.py 进程清理不可靠
- **模块**: run.py _cleanup()
- **问题**: 启动时用 wmic 杀旧进程，但 wmic 输出格式在中文 Windows 上解析失败（中文路径乱码），旧进程没杀掉，新 Web 绑不上端口 8000。
- **根因**: wmic 输出的 CommandLine 列在中文 Windows 上有编码问题。
- **修复建议**: 改用 `taskkill /F /IM python.exe` + `netstat -ano | findstr :8000` 两段式清理。

#### 3. 🟢 run.py 用 sys.executable 启动子进程
- **模块**: run.py
- **问题**: run.py 里 `subprocess.Popen([sys.executable, "main.py"])`，sys.executable 取决于用户怎么运行 run.py（venv 还是系统 Python）。
- **根因**: 硬编码路径问题。
- **修复建议**: 固定用 `[".\\venv\\Scripts\\python.exe", "main.py"]`。

---

### 二、Web API 问题

#### 4. 🔴 /api/trades 查询不存在的列
- **模块**: src/web/server.py, get_trades()
- **问题**: SQL 查询 `SELECT ... profit, close_time, hold_minutes FROM decisions`，但 decisions 表没有这三列。
- **根因**: 重写 server.py 时从旧代码复制了错误的列名。
- **修复状态**: ✅ 已修复（改为 cooldown 列），但 fix 脚本跑在旧文件上被覆盖过。
- **修复建议**: 确认 server.py 里 get_trades 的 SQL 只查 decisions 表真实列: id, timestamp, direction, strategy, price, volume, order_id, reason, indicators, cooldown, created_at。

#### 5. 🟡 /api/trades/stats 返回 wins: null
- **模块**: src/web/server.py, _load_trade_stats()
- **问题**: trades 表为空时 SUM 返回 NULL，导致 wins = null，前端 `wins.toFixed()` 报错。
- **修复状态**: ✅ 已修复（加了 COALESCE）。
- **修复建议**: 确认 `_load_trade_stats()` 里 wins 和 total_pnl 都包了 COALESCE(..., 0)。

#### 6. 🟢 缺少放置订单的 API
- **模块**: src/web/server.py
- **问题**: 前端只能查看持仓和 Killswitch，没办法通过 API 手动下单。
- **修复建议**: 新增 `POST /api/order` 端点，接收 direction, volume, sl, tp 参数。

#### 7. 🟢 缺少健康检查端点
- **模块**: src/web/server.py
- **问题**: 没有 `/health` 或 `/ping` 端点用于基础连通性检查。
- **修复建议**: 加一个不依赖 MT5 的简单端点。

#### 8. 🟡 CORS 配置在错误响应时可能失效
- **模块**: src/web/server.py
- **问题**: CORS 中间件配了，但如果 handler 抛出 500 异常，某些情况下 CORS header 可能没加上。
- **修复建议**: 确认 FastAPI 的 CORSMiddleware 在异常处理链最外层。

---

### 三、MT5 连接问题

#### 9. 🔴 Web 服务的 MT5 连接缓存导致看不到最新持仓
- **模块**: src/web/server.py, _ensure_mt5()
- **问题**: _ensure_mt5() 初始化 MT5 一次后用 `global _mt5_inited` 缓存，之后的请求不再重新连接。如果连接过期或 MT5 终端重启，API 返回过时数据（balance/equity 不更新，positions 为空）。
- **根因**: `if _mt5_inited: return` 跳过了所有后续初始化。
- **修复建议**: 
  方案A: 每次 API 请求都重新初始化 MT5（性能略差但数据最新）。
  方案B: 加一个心跳机制，每60秒检查 mt5.account_info() 是否有效。
  方案C: 把 `_mt5_inited` 改成带超时的缓存（比如10分钟重新连接一次）。

#### 10. 🟠 MT5 连接丢失后无重试机制
- **模块**: 所有调用 mt5 的地方
- **问题**: 如果 MT5 终端崩溃或网络断开，try/except 只打日志，不重连。
- **修复建议**: 封装一个带重试的 mt5_executor 方法。

#### 11. 🟡 XAUUSD 只允许做空 (trade_mode=4)
- **模块**: 交易配置
- **问题**: 当前 MT5 账户上 XAUUSD 的 trade_mode 是 4（SHORT_ONLY），不能下买单。
- **根因**: TradeMaxGlobal-Demo 账户类型限制。
- **修复建议**: 下单前检查 `mt5.symbol_info("XAUUSD").trade_mode`，如果是 4 只下卖单。

---

### 四、B2 策略问题

#### 12. 🔴 B2 原版是纯模拟策略，从不实际下单
- **模块**: src/strategy/engine_b2.py
- **问题**: 旧版 B2 用 `self._po = {"d":"l","e":cl,...}` 在内存里管理虚拟仓位，用 K 线 OHLC 判断虚拟 SL/TP 是否触发，从不调用 `self._exe.buy_market()`。所以跑一天都不会有真实 MT5 订单，也不会发邮件。
- **根因**: 策略写成了「回测模式」，只算数学盈亏，不操作真实账户。
- **修复状态**: ✅ 已重写，现在收到信号会调用 `self._exe.buy_market(sl=xx, tp=xx)` 真实下单。
- **修复建议**: 确认 `_process_candle()` 里 la/or lb/or lc/sa/or sb/or sc 条件满足时确实调用了 `_open_order()`。

#### 13. 🟡 B2 get_status() 返回 "B2" 而不是 "RUNNING"
- **模块**: src/strategy/engine_b2.py
- **问题**: `get_status()` 返回 `{"state": "B2", ...}`，main.py 写入共享状态文件时 `st.get("state", "RUNNING")` 拿到了 "B2"，导致前端 engine_state 显示 "B2" 而不是 "RUNNING"。
- **修复状态**: ✅ 已修复。
- **修复建议**: 确保所有策略的 get_status() 返回 state 为 RUNNING/SLEEP/KILLED/ERROR 之一。

#### 14. 🟡 B2 的每日计数器不持久化
- **模块**: src/strategy/engine_b2.py
- **问题**: `_trade_count`, `_daily_pnl`, `_daily_loss` 存在内存中。引擎进程重启后会重置。
- **修复建议**: 用 SQLite 或 JSON 文件持久化每日数据。

#### 15. 🟢 B2 只在 M15 周期运行
- **模块**: src/strategy/engine_b2.py
- **问题**: TickAggregator 聚合到 M15，信号也只在 M15 收盘时触发。反应速度较慢。
- **建议**: 策略本身就是中低频，这个不是 bug 是设计。如果要改可以改成 M5。

---

### 五、Python 字符串转义问题（重要！）

#### 16. 🔴 Python 三引号字符串将 `\`` 和 `\$` 保留反斜杠
- **模块**: 所有用 Python 三引号 `"""..."""` 写 JavaScript 代码的文件
- **问题**: 在 Python 3.11 中，`\$` 和 `\`` 不是合法的转义序列。Python 保留反斜杠，导致文件里出现 `\${name}` 和 `\`template literal\`` 而不是 `${name}` 和 `` `template literal` ``，JavaScript 无法执行。
- **具体表现**: 
  - `\`` → 模板字面量变成普通字符串
  - `\$` → 模板表达式变成普通美元符号
  - 页面要么空白，要么报 `SyntaxError: Invalid or unexpected token`
- **影响文件**: src/web/static/index.html（之前已修）、任何用 Python 写 JS 的场景
- **修复建议**: 
  - 写 JS/HTML 时用 `$` 而不是 `\$`
  - 或者用 raw string `r"""..."""`
  - 或者写完后用自动化脚本全局替换 `\` → `(空)` 对 `\` 和 `\$`

---

### 六、编码与文件问题

#### 17. 🟠 UTF-8 BOM 导致 SyntaxError
- **模块**: 多个 Python 源文件
- **问题**: PowerShell 的 `Out-File -Encoding UTF8` 默认加 BOM（Byte Order Mark `\\xef\\xbb\\xbf`），Python 解释器在文件开头遇到 BOM 时在某些版本会报 SyntaxError。
- **根因**: Windows PowerShell 5.1 的 Out-File 默认带 BOM，PowerShell 7+ 才有 UTF8NoBOM。
- **修复建议**: 写 Python 文件时用 `-Encoding UTF8NoBOM`（PowerShell 7+）或通过 `python -c "open('x.py','w',encoding='utf-8').write(code)"` 写入。

#### 18. 🟡 项目路径含中文导致 PowerShell 命令异常
- **模块**: 所有 shell 命令
- **问题**: 路径 `C:\\Users\\笨猫\\Desktop\\MT4-Ai` 含中文，某些 PowerShell 命令和 Python subprocess 调用会编码异常。
- **修复建议**: 涉及路径操作的脚本加 `encoding='utf-8'` 参数；避免用管道传中文路径。

#### 19. 🟢 __pycache__ 缓存导致代码修改不生效
- **模块**: 所有 Python 模块
- **问题**: 修改 .py 源文件后，`__pycache__/*.pyc` 可能还是旧版本，如果时间戳匹配，Python 直接加载 pyc 不重编译。
- **修复建议**: 每次部署前删所有 `__pycache__` 目录。

---

### 七、邮件通知问题

#### 20. 🟡 静默预警的 "1小时" 硬编码
- **模块**: src/notifier/email_alerter.py
- **问题**: 静默预警硬编码 3600 秒，不区分交易时段（周末、非交易时间）。
- **修复建议**: 
  方案A: 改为仅在交易时段（7-20 UTC）计算静默时长。
  方案B: 做成可配置参数。

#### 21. 🟡 邮件发送失败无重试
- **模块**: src/notifier/email_alerter.py _send()
- **问题**: try/except 只打日志，失败就丢了。如果 SMTP 临时不可达，通知丢失。
- **修复建议**: 加 retry 机制（如重试3次，间隔5秒）。

---

### 八、前端问题

#### 22. 🟡 static/index.html 是纯 JS 版本，不匹配用户 React 前端
- **模块**: src/web/static/index.html
- **问题**: 用户有一个 React TypeScript 前端 (`E:/电商/mt5.tsx`)，但由于 Node.js 不可用，只能写一个等价纯 JS 版本。两者功能相同但 UI 不完全一致。
- **修复建议**: 让用户自己构建 React 前端并部署到 static 目录。

#### 23. 🟢 仪表盘只轮询状态和持仓，不显示交易历史
- **模块**: static/index.html
- **问题**: 新仪表盘只显示账户状态、每日目标和活跃持仓，没有交易历史表和复盘图表。
- **修复建议**: 加上交易记录表格和 K 线复盘功能。

---

### 九、数据库问题

#### 24. 🟡 decisions 表和 trades 表数据分离
- **模块**: src/strategy/recorder.py
- **问题**: 策略记录决策到 decisions 表，MT5 记录成交到 trades 表，两者没有外键关联。无法直接从决策追踪到实际成交。
- **修复建议**: 在 decisions 表加一个 trade_id 字段关联到 trades 表。

#### 25. 🟢 数据库查询没有使用索引
- **模块**: src/web/server.py
- **问题**: decisions 和 trades 表没有在 timestamp 或 order_id 上建索引，大量数据后查询会慢。
- **修复建议**: `CREATE INDEX idx_decisions_timestamp ON decisions(timestamp)`。

---

### 十、杂项

#### 26. 🟢 Excel/文档模板问题
- **模块**: 文档/模板文件
- **问题**: `__fix_executor.py` 等临时脚本提交到目录。
- **修复建议**: 清理所有 `__*.py` 临时文件（我在最后已经清理了）。

#### 27. 🟢 QQ邮箱 SMTP 可能因频率限制发不出去
- **模块**: src/notifier/email_alerter.py
- **问题**: QQ 邮箱 SMTP 有限频策略，短时间内多发会被临时封禁。
- **修复建议**: 加一个发送间隔检查（同一封邮件至少间隔60秒再发）。

---

## 对修复智能体的工作指引

1. **代码审查优先级**: 🔴 > 🟠 > 🟡 > 🟢
2. **先修架构问题**（1, 2, 9）— 这些影响系统稳定性
3. **再修 API 问题**（4, 5, 8）— 这些影响前端正常显示
4. **MT5 连接问题**（10, 11）— 这些影响交易的可靠性
5. **策略问题**（12-15）— 这些影响交易策略正常运行
6. **编码问题**（16-19）— 这些影响代码修改后是否能正常工作

---

## 关键文件清单

| 文件 | 作用 | 当前状态 |
|------|------|----------|
| run.py | 启动器，启动引擎+Web | 进程清理有缺陷 |
| main.py | 引擎入口，跑B2策略 | 基本稳定 |
| web.py | 启动FastAPI | 简单，少改 |
| src/web/server.py | API路由 | 有若干bug需要确认修复 |
| src/strategy/engine_b2.py | B2交易策略 | 从模拟→实盘已重写 |
| src/trading/mt5_executor.py | MT5下单封装 | SL/TP支持已加 |
| src/notifier/email_alerter.py | QQ邮箱通知 | 无重试机制 |
| src/web/static/index.html | 前端仪表盘 | 纯JS版，无编译依赖 |
