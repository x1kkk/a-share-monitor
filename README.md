# A 股主板异动与涨停盯盘

单进程 FastAPI 后端 + Vue 3 / Vite / TailwindCSS 前端。所有状态在内存中，必须使用 `--workers 1`。默认 `MOCK_MODE=true`；页面始终标注模拟或实盘。

## 代码协作

私有仓库：<https://github.com/x1kkk/a-share-monitor>。初始化后，所有修改通过独立分支提交 Pull Request 到 `main`，不直接推送主分支；PR 说明改动内容和验证结果。完整规则见 `AGENTS.md`。

仓库保存前后端源码、测试和配置示例；本地环境、缓存、构建产物、运行抓取数据及旧自检报告不纳入版本管理。

## 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
cd frontend
npm install
npm run build
cd ..
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
```

构建完前端再启动后端，浏览器打开 `http://127.0.0.1:8000/`。FastAPI 自动提供 `frontend/dist/index.html` 和 `/assets`。开发时可在另一终端执行 `cd frontend; npm run dev`，Vite 将 `/api`、`/ws`、`/health` 代理到 `127.0.0.1:8000`。

当前工作站已有 `.venv` 的 `_ssl` DLL 加载失败，但系统 Python 的 SSL 正常。无需修改业务代码，可在项目根目录临时使用系统 Python 加载现有依赖启动：

```powershell
python -X utf8 -c "import ssl,sys,os,runpy; site=os.path.abspath('.venv/Lib/site-packages'); sys.path.insert(0,site); os.environ['PYTHONPATH']=site; sys.argv=['uvicorn','main:app','--host','127.0.0.1','--port','8000','--workers','1']; runpy.run_module('uvicorn',run_name='__main__')"
```

这是本机环境的启动替代方式；Linux 部署使用正常虚拟环境。不要关闭 TLS 校验来绕过此问题。

## 运行配置

`.env.example` 是配置示例，应用本身不自动读取 `.env`。请通过服务管理器或 shell 设置环境变量。PowerShell 示例：

```powershell
$env:MOCK_MODE = 'false'
$env:API_TOKEN = '请替换为足够长的随机密钥'
$env:CORS_ORIGINS = 'http://127.0.0.1:8000,http://localhost:5173,https://monitor.example.com'
```

`MOCK_MODE=false` 且交易日历处于 09:15–11:30、13:00–15:00 时进入 LIVE。休市时自动模拟；日历不可用时 LIVE 阻断，不使用星期推断。关闭企业微信时将 `WECHAT_WEBHOOK_URL` 留空；默认不会向真实群发送模拟事件。

生产应设置 `API_TOKEN`，在页面连接设置中输入相同 Token。浏览器经 `/ws` 的第一条 JSON 消息认证，REST 使用 Bearer 请求头；Token 不写入前端构建产物或 URL。

## 香港静态前端 + 大陆后端

1. 在构建机执行 `cd frontend && npm ci && npm run build`，将 `frontend/dist/` 内容复制到香港 Nginx 的 `/var/www/market-monitor/frontend/dist/`。
2. 大陆机器建立 Linux 虚拟环境、安装 `requirements.txt`，设置 `MOCK_MODE=false`、`API_TOKEN`、`CORS_ORIGINS=https://你的前端域名`。以服务启动：

   ```sh
   .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
   ```

3. 根目录 `nginx.conf` 是完整配置。替换 `monitor.example.com`、证书路径、静态目录和 `10.0.0.10:8000`（大陆私网/VPN地址）。保留原始浏览器 Origin；后端允许的是香港页面域名，与转发的 Host 不同。
4. 大陆 `8000` 端口仅允许香港代理/私网访问。香港端终止 HTTPS；若两端经公网通信，应将该链路改为经过验证的 HTTPS 或 VPN。
5. 在香港部署机器运行 `nginx -t`，通过后再 reload。本工作站没有 Nginx，未代替目标服务器执行配置验证或部署。

`/ws` 已包含 HTTP/1.1 升级头、`proxy_read_timeout 3600s`、`Host $proxy_host` 和关闭代理缓冲。前端使用同源相对地址，无需硬编码大陆 IP。

## 字段口径与生命周期

- 实盘主源为腾讯 `qt.gtimg.cn`，每批最多 650 只，并发请求；3050 只为 5 个行情请求。行情同包解析买卖一盘口，后续 `fetch_books()` 复用同轮结果。资金流 `ff_` 为另 5 个并发请求，缺失/超时不阻塞主行情；全部缺失时每分钟探测一次。部分行情批次失败、缺码、重复、畸形字段时拒绝整轮，不推进资金流基线或状态机。单轮沿用现有 1.85 秒总预算、2 秒调度；实际延迟需在部署机验证，不承诺 200ms。
- 股票池按沪深两市场分别读取交易所代码表及上市日期，失败改用新浪分页代码表；首次缺少上市日期时，以新浪公司基础资料补全。`universe_cache.json` 保存完整非 ST、非退市的 00/60 主板池、上市日期及首次观察日期，原子写入；每市场至少 1000 只并检查分页总数与去重，有完整旧缓存时拒绝突然缩水超过 5% 的新结果。新源不可用保留该市场最近完整缓存，不以 `demo_seed` 代替实盘股票池。冷启动补资料可能需约一分钟，请提前启动；当日缓存直接复用，已验证日期不重复查询。上市日期未知且观察不足 15 个交易日的个股继续严格过滤，不能伪造上市资格。
- 日历独立使用 AKShare 新浪交易日历并缓存至 `market_calendar_cache.json`。网络失败时仅接受覆盖当天的缓存，不以工作日推断开市。初始化不再请求东财涨停池；无昨日真实连板资料时 `streaks_ready=false`、连续涨停数显示“—”，不影响实盘启动。实盘过滤跨日旧报价，同日静默报价仍保留；盘后模拟可以使用上一交易日的真实静态种子。
- 第一栏使用后端 `OrderedDict` 顺序；120 秒变灰，满 420 秒移除。达到涨停价立即移出（以收到该股票成功报价的轮次为准）。临时缺股保留最后值和入榜时钟，`quote_missing=true` 标记待更新；旧值不参加当轮截面排名。
- 腾讯集合竞价允许成交量为零；价格为零表示尚无有效报价，该股当轮不参与排名。负成交量、无效价格、涨跌幅、成交额等拒绝整轮。保留东财旧适配器用于离线分页审计，默认实盘链路不调用它。
- 腾讯行情成交额 `[37]×10000`、流通市值 `[44]×1e8`，单位元；成交量及买卖一量均为手。`ff_` 的 `[3]×10000` 是主力净流入，要求代码和 `[13]` 交易日期匹配。缺失时使用快照估计：`Δ成交额 × (Δ主动买量−Δ主动卖量)/(Δ主动买量+Δ主动卖量)`，主动量取 `[7]`/`[8]`，首个快照以当日累计量额作粗估；主动量缺失/无增量则保守计零增量。该值不是交易所逐笔主力统计，`flow_source=snapshot_estimate`，界面标“（估）”；`tencent_ff` 表示可用的腾讯资金流。
- 快照无法唯一还原超大单/大单真实分类，因此两者始终 `split_estimated=true` 并在界面标“（估）”。按每轮成交额增量的启发式权重 `w=0.4+0.2×min(Δ成交额/1000000,1)` 分配主力净额增量，保持 `super_inflow+large_inflow=main_inflow`；不将估算包装成真实分类资金。跨日、昨收变化、量额计数回退或资金流来源切换时递增 `flow_epoch`，清空两分钟窗口，避免基线跳变触发虚假异动。保留旧东财适配器的 `f62/f66/f72` 口径。`total_net_inflow=super_inflow+large_inflow`，不重复加主力。
- `seal_amount=price*buy_one_lots*100`，单位元；`seal_float_pct` 已是百分数。盘口缺失时封单金额可能为 null，页面显示“—”。集合竞价撮合价达到涨停价仍沿用现有封板状态机语义，不额外声称为已成交封板。
- 2 分钟涨速沿用 `100*(当前价-窗口最早价)/昨收价`；窗口不足两分钟可触发，`window_seconds` 给出实际跨度。此项未在本次任务内重定义。
- 事件具有独立 id；客户端有界去重，重连初始快照不回放旧事件。声音必须点击启用，不会自动突破浏览器播放限制。

## 我的自选股

点击第三页签“★ 我的自选股”，输入 6 位代码或中文名称，从搜索结果一键添加。右侧全量看板包含所有自选，涨停置顶，可直接删除；左侧独立雷达显示急拉、首次封板、炸板，每只股票保留最近一次事件，120 秒变灰、300 秒移除，新事件刷新该股的雷达时钟并置顶。普通异动榜仍使用原有 420 秒时限。

自选急拉条件为窗口涨速至少 2% 且当前主力净流入为正，按条件从不满足到满足触发；不受普通榜每分钟 10 条限额，也不占用普通名额。即使已在普通异动榜上，也能再次触发独立雷达。涨速窗口口径沿用现有实现，实际跨度见 `window_seconds`。自选告警在 Notifier 中优先发送并绕过普通 300 秒冷却，前端仍按事件 ID 去重；VIP Toast 保留 12 秒、金边红框，在其他页签显示可点击的置顶横幅。声音仍需手动开启，爆发事件采用有界音频排队并优先提示 VIP。

`watchlist.json` 在后端根目录自动创建，仅保存代码数组，重启后恢复；使用临时文件、flush/fsync、原子替换，写入成功后才更新内存。文件内容损坏时拒绝覆盖并停止启动，应修复文件后重启。该文件和临时文件均不提交 Git，生产部署时保留它；这是单进程实例共享的一份自选列表，不是多用户账户配置。

接口均沿用 `API_TOKEN` 认证，写入还校验浏览器 Origin：

| 接口 | 行为 |
| --- | --- |
| `GET /api/watchlist` | 返回全部自选行情，缺失报价明确标记 |
| `POST /api/watchlist` | 请求 `{"code":"600036"}`，返回内存中的初始行情；重复添加幂等 |
| `DELETE /api/watchlist/{code}` | 移除自选，重复删除幂等 |
| `GET /api/stocks/search?q=招商` | 对代码或名称进行子串匹配，最多 30 条 |

添加和搜索均基于真实行情缓存与真实交易日历，校验成熟的非 ST、00/60 主板股票；真实元数据不可用时拒绝新添加，不使用模拟种子伪造上市资格。已保存代码仍保留在看板中。WebSocket 每轮新增 `watchlist_anomalies` 和 `watchlist_leaders` 两个数组，管理操作也立即广播同步。

专项回归：`python -m unittest -v test_watchlist`（请使用已安装项目依赖的 Python）。

## 自检

```powershell
.\.venv\Scripts\python.exe engine.py
.\.venv\Scripts\python.exe -m unittest -v test_backend
.\.venv\Scripts\python.exe -m unittest -v test_watchlist test_data_sources
cd frontend
npm test
npm run build
```

若当前工作站虚拟环境 SSL 导致测试客户端不可用，可使用系统 Python 加载已安装依赖运行后端回归：

```powershell
python -X utf8 -c "import ssl,sys,runpy; sys.path.insert(0,r'.venv/Lib/site-packages'); sys.argv=['unittest','-v','test_backend']; runpy.run_module('unittest',run_name='__main__')"
```

`test_backend.py` 覆盖 09:27 静默报价、原状态机边界、缺失股票保留、涨停移出、资金合计、WS 连接断开/取消的任务回收，以及带 Token 的权重更新。旧《后端实现与自检报告》记录的是修改前状态，其源码及测试结果不能代表当前版本。
