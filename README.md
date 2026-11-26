# A 股主板异动与涨停盯盘

单进程 FastAPI 后端 + Vue 3 / Vite / TailwindCSS 前端。行情状态机使用内存，真实快照与基础资料使用原子磁盘缓存，必须使用 `--workers 1`。生产使用 `MOCK_MODE=false`，页面只展示真实行情；只有显式设置 `MOCK_MODE=true` 才启用标注清楚的模拟演练，不会因休市或行情失败自动切入模拟。

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

`MOCK_MODE=false` 且交易日历处于 09:15–11:30、13:00–15:00 时进入 LIVE；显式 `MOCK_MODE=true` 则使用模拟引擎。休市、午休和盘前展示最近已验证的真实收盘快照，保留快照时间并冻结倒计时，不发送历史告警。无真实快照时显示等待数据，日历不可用时阻断实盘，不按星期猜开市。关闭企业微信时将 WECHAT_WEBHOOK_URL 留空。

生产应设置 `API_TOKEN`，在页面连接设置中输入相同 Token。浏览器经 `/ws` 的第一条 JSON 消息认证，REST 使用 Bearer 请求头；Token 不写入前端构建产物或 URL。

## 一期升级：多空异动与过滤设置

普通异动支持 `rapid_rise`（急速拉升、多头）和 `heavy_plunge`（放量跳水、空头）。后端是方向、标签、触发原因、评分与排序的唯一来源；前端“全部 / 仅多头 / 仅空头”仅做保序展示筛选。

- 新增 `speed_1m`，公式为 `100*(当前价-60秒内最早价)/昨收价`，`window_1m_seconds` 给出实际跨度；只有一个样本时返回 null。保留 `speed_2m`、`inflow_2m`、`window_seconds` 兼容字段，新增信号的资金增量使用 `inflow_1m`。
- `vol_ratio` 为最近至多 60 秒的成交量速率，除以此前至多约 20 分钟、不重叠基线的速率；只使用启动后累计成交量之差，不把开机前的当日累计量当成一笔成交。基线按 10 秒采样，最多约 128 点/股票；不扩展逐笔数据源。
- 总观察不足 120 秒、基线不足 60 秒或基线速率为零时，量比为 null，按涨跌速与资金条件降级，`reason` 明示“量比基线未就绪”。量比就绪后必须达标。切换 `flow_epoch`、量额回退、昨收变化、超过 10 秒报价间断以及切换实盘会重置短窗/基线。
- 普通拉升要求窗口主力净流入为正且未涨停；普通跳水要求窗口主力净流入为负且未跌停。自选拉升沿用累计主力净流入为正；自选跳水允许窗口或累计净流入为负。缺失报价保留旧行但不参加当轮评分。
- 普通多头、空头每分钟各最多 10 只，自选不占额度。评分只在相同方向的当前候选内计算；多头沿用五项权重，以 1 分钟速度作为速度指标；空头五个权重槽位依次用于跌速绝对值、主力净流出、换手率、量比、成交额。空头缺量比时按其余可用指标权重重新归一化；所有正权重指标均不可用时给中性 50 分，不伪造量比。
- 普通卡片保存触发时方向、原因、短窗指标与评分，行情价格继续刷新；同股反向新信号可重新入榜并占对应方向额度。同方向持续条件不会每轮刷新卡片。自选雷达保留每股最新事件，原首次封板/炸板/回封状态转移不变，同日回封不再生成首次封板事件。

`GET /api/filters` 与 `PUT /api/filters` 都需要现有 Bearer 认证，并检查已提供的浏览器 Origin。PUT 请求示例：

```json
{"sensitivity":"balanced","exclude_st":false,"exclude_low_amount":false,"min_daily_amount":100000000}
```

三档速度/量比阈值为：保守 `3% / 3倍`、均衡 `2% / 2倍`、灵敏 `1% / 1.5倍`。金额门槛为整数元；`min_daily_amount` 可省略，默认 1 亿。两个噪音开关只过滤普通异动扫描，不过滤自选、股票搜索或涨停看板。旧股票池缓存会尝试刷新一次以纳入 ST；数据源失败仍保留完整旧池，补齐前不能宣称包含全部 ST。

设置先以临时文件、flush/fsync、原子替换写入 `monitor_filters.json`，成功后才同步 live/mock 两引擎并广播；写失败返回 503 且内存不变，损坏配置拒绝启动以避免覆盖。部署时保留该文件及 `watchlist.json`，不要提交 Git。灵敏度和过滤设置跨重启恢复，评分权重仍只在内存。

响应和快照顶层新增 `filters`（包含生效阈值）与 `ttl`：

```json
{"ttl":{"anomalies":{"active_seconds":120,"remove_seconds":420},"watchlist":{"active_seconds":120,"remove_seconds":300}}}
```

异动行/事件新增 `direction`、`signal_type`、`signal_label`、`signal_tags`、`reason`、`speed_1m`、`window_1m_seconds`、`inflow_1m`、`vol_ratio`、`alert_level`；原 `event_type=anomaly/limit_up/break_open` 保留。`minute_admitted` 保持多头兼容计数，`minute_admitted_by_direction` 返回两个独立计数。通知冷却按具体信号隔离，避免拉升压制同股跳水告警；微信包含方向和原因。

前端显示 `--` 表示未知，进度条读取服务端 TTL、本地平滑推进和变灰，由后端控制股票移除。点击“异动设置”选择档位与过滤开关；“声音提醒”需点击授权，level 1/2/3 分别合成一声/两声/三声，空头使用更低音高。首次封板、炸板及自选强异动为 level 3；普通双向异动为 level 2。仍保留有限音频队列与重连不重放约束。

模拟验收：显式设置 `MOCK_MODE=true`，构建前端并以单 worker 启动，打开页面点击开启声音、展开异动设置。均衡档约前 30 秒出现红色拉升及绿色跳水卡，随后首次封板/炸板；约 160 秒还会出现量比已就绪的跳水场景，整体约 8 分钟循环。观察原因、`--` 冷启动量比、方向筛选和设置保存；自选需真实元数据与交易日历准备好后添加，模拟种子不能用来伪造新增自选资格。

## 二期升级：五档盘口与个股轨迹

腾讯同轮快照新增 `Book.bids/asks`（元、手），空的价量档位忽略，`bid_levels` 保留过滤前的真实买档序号；`Quote.outer_vol/inner_vol` 为累计手数，缺失为 null。原 `Book` 前四个位置参数不变。实盘复用已解析盘口，不为全市场另发盘口请求。

- `large_order_support`：买一至买五中某档金额至少 **1000 万元**，手数至少为其余有效买档均量的 **5 倍**（至少存在一个其他有效档），同一两位小数价位连续 **30 秒**满足才触发。使用单调时钟；撤单、跌破门槛、缺盘口/报价、切源、量额/内外盘计数回退或断流超过 10 秒重置。已涨停及涨停价买单不算托底，同一连续条件不重复触发。
- `suspected_active_buy` / `suspected_large_smash`：相邻快照间隔 `0 < dt <= 6秒`，成交额增量至少 **500 万元**，相对上一帧价格上冲至少 **0.3%** / 回落至少 **0.3%**，外盘 / 内盘增量超过另一侧 **1.5 倍**。内外盘缺失时使用主力资金净额增量正负降级并明示；首帧和异常重置只建立基线。分别排除已涨停 / 已跌停，始终带 `signal_estimated=true`、`疑似` 和 `快照估算`，不代表逐笔或十档数据。
- 新盘口信号普通声音 level 1，自选 level 2；一期双向异动保持普通 level 2、自选 level 3，首次封板/炸板 level 3。多空评分与额度独立；同方向卡片出现新种类时追加轨迹、更新触发说明，不重复占额度或延长普通卡片 TTL；同轮多个信号均记录。

股票行新增 `outer_vol`、`inner_vol`、`outer_inner_ratio`，后者在计数完整且内盘大于零时取两位小数，否则 null。信号新增 `signal_estimated`、`support_info`（档位 `level`、价格 `price`、金额元 `amount`、持续秒数 `duration_seconds`）与 `summary`。`timeline` 为倒序最近 8 条，每股内存最多 12 条，包含 `time/direction/signal_type/signal_label/price/change_pct/summary/is_estimated`；跨交易日或清理引擎窗口时清空，临时缺报价保留历史。

过滤接口和 `monitor_filters.json` 新增：

```json
{"enable_orderbook_signals":true,"support_min_amount_wan":1000.0,"burst_min_amount_wan":500.0}
```

金额单位万元，必须为有限数字且 `0 < value <= 1000000`。一期四字段配置自动补默认值；旧 PUT 不带新字段时保留现值。关闭盘口信号会清理对应基线和计时器，仍展示内外盘比和已有轨迹。页面“异动设置”可保存这些参数，卡片底部“异动轨迹”默认折叠；估算微标提供口径说明。

**ST 口径按交易日处理**：历史主板 ST/`*ST` 在 2026-07-06 前为 ±5%；此日起按现行 ±10%。`config.stock_limit_prices()` 统一封板、跌停和盘口排除口径，价格按分四舍五入。依据：[上交所 2026 交易规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml)、[深交所 2026 交易规则](https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf)。不能将当前 ST 固定按旧 5% 计算。

模拟验收：设置 `MOCK_MODE=true`，使用默认均衡档和默认盘口门槛，重新启动单 worker，从首轮模拟行情计时（调度可能偏移约一轮 2 秒）：

| 时间 | 股票 | 预期 |
| --- | --- | --- |
| 30 秒 | 002594 比亚迪 | 买二约 1200 万持续托底，普通提示一级 |
| 40 秒 | 002594 | 疑似盘口扫货，估算微标，轨迹增至 2 条 |
| 44 秒 | 003816 中国广核 | 疑似大单砸盘，绿色卡与估算微标 |
| 48 秒 | 003816 | 放量跳水，轨迹增至 2 条 |
| 52 秒 | 002594 | 急速拉升，同卡轨迹增至 3 条，普通提示二级 |

展开轨迹应按最近在前展示；同期涨停股票卖档为空且不产生托底。约 480 秒循环，新周期重建基线。改灵敏度、金额门槛或噪音过滤后，触发时点可能变化；自选需先有真实元数据和交易日历才可添加。`test_data_sources.SourceBoundaryChecks.test_simulator_engine_emits_exact_orderbook_timeline` 无等待地验证以上完整时序。

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

升级时同时更新匹配的前后端版本，保持 `--workers 1` 与生产 `MOCK_MODE=false`。部署前保留 `.env`、`watchlist.json`、`monitor_filters.json` 以及 `universe_cache.json`、`market_calendar_cache.json`、`sector_cache.json`、`company_sectors.json`、`market_snapshot.json`；不要用构建目录覆盖这些服务端运行文件。真实缓存按各自校验恢复，损坏的自选或过滤配置应先修复，不能通过覆盖空文件绕过启动检查。

启动或重启后检查 `/health`、带认证的 `/api/snapshot` 与 WebSocket 首帧：交易时段为 LIVE；休市为 `closed`（尚无真实快照时为等待状态）；短暂行情异常可为 `recovering`，并通过 `using_cached_snapshot` 标明沿用真实快照。核对快照日期、`close_complete`、行业标签、过滤设置及自选恢复，刷新/重连不应回放历史声音。源站故障时保留最近有效快照并重试，不将模拟数据写入 `market_snapshot.json`。


## 字段口径与生命周期

- 实盘主源为腾讯 `qt.gtimg.cn`，每批最多 650 只，并发请求；3050 只为 5 个行情请求。行情同包解析买卖一盘口，后续 `fetch_books()` 复用同轮结果。资金流 `ff_` 为另 5 个并发请求，缺失/超时不阻塞主行情；全部缺失时每分钟探测一次。部分行情批次失败、缺码、重复、畸形字段时拒绝整轮，不推进资金流基线或状态机。单轮沿用现有 1.85 秒总预算、2 秒调度；实际延迟需在部署机验证，不承诺 200ms。
- 股票池按沪深两市场分别读取交易所代码表及上市日期，失败改用新浪分页代码表；首次缺少上市日期时，以新浪公司基础资料补全。`universe_cache.json` 保存完整非退市的 00/60 主板池（包含 ST，由普通异动过滤开关控制）、上市日期及首次观察日期，原子写入；每市场至少 1000 只并检查分页总数与去重，有完整旧缓存时拒绝突然缩水超过 5% 的新结果。新源不可用保留该市场最近完整缓存，不以 `demo_seed` 代替实盘股票池。冷启动补资料可能需约一分钟，请提前启动；当日缓存直接复用，已验证日期不重复查询。上市日期未知且观察不足 15 个交易日的个股继续严格过滤，不能伪造上市资格。
- 日历独立使用 AKShare 新浪交易日历并缓存至 `market_calendar_cache.json`。网络失败时仅接受覆盖当天的缓存，不以工作日推断开市。初始化不再请求东财涨停池；无昨日真实连板资料时 `streaks_ready=false`、连续涨停数显示“--”，不影响实盘启动。实盘过滤跨日旧报价，同日静默报价仍保留；实盘模式下盘前与盘后沿用真实收盘快照，不再使用模拟种子或自动回退模拟；显式模拟演练仅按一期验收流程开启。
- 第一栏使用后端 `OrderedDict` 顺序；120 秒变灰，满 420 秒移除。达到涨停价立即移出（以收到该股票成功报价的轮次为准）。临时缺股保留最后值和入榜时钟，`quote_missing=true` 标记待更新；旧值不参加当轮截面排名。
- 腾讯集合竞价允许成交量为零；价格为零表示尚无有效报价，该股当轮不参与排名。负成交量、无效价格、涨跌幅、成交额等拒绝整轮。保留东财旧适配器用于离线分页审计，默认实盘链路不调用它。
- 腾讯行情成交额 `[37]×10000`、流通市值 `[44]×1e8`，单位元；成交量及买卖一量均为手。`ff_` 的 `[3]×10000` 是主力净流入，要求代码和 `[13]` 交易日期匹配。缺失时使用快照估计：`Δ成交额 × (Δ主动买量−Δ主动卖量)/(Δ主动买量+Δ主动卖量)`，主动量取 `[7]`/`[8]`，首个快照以当日累计量额作粗估；主动量缺失/无增量则保守计零增量。该值不是交易所逐笔主力统计，`flow_source=snapshot_estimate`，界面标“（估）”；`tencent_ff` 表示可用的腾讯资金流。
- 快照无法唯一还原超大单/大单真实分类，因此两者始终 `split_estimated=true` 并在界面标“（估）”。按每轮成交额增量的启发式权重 `w=0.4+0.2×min(Δ成交额/1000000,1)` 分配主力净额增量，保持 `super_inflow+large_inflow=main_inflow`；不将估算包装成真实分类资金。跨日、昨收变化、量额计数回退或资金流来源切换时递增 `flow_epoch`，清空两分钟窗口，避免基线跳变触发虚假异动。保留旧东财适配器的 `f62/f66/f72` 口径。`total_net_inflow=super_inflow+large_inflow`，不重复加主力。
- `seal_amount=price*buy_one_lots*100`，单位元；`seal_float_pct` 已是百分数。盘口缺失时封单金额可能为 null，页面显示“--”。集合竞价撮合价达到涨停价仍沿用现有封板状态机语义，不额外声称为已成交封板。
- 2 分钟涨速沿用 `100*(当前价-窗口最早价)/昨收价`；窗口不足两分钟可触发，`window_seconds` 给出实际跨度。此项未在本次任务内重定义。
- 事件具有独立 id；客户端有界去重，重连初始快照不回放旧事件。声音必须点击启用，不会自动突破浏览器播放限制。

## 我的自选股

点击第三页签“★ 我的自选股”，输入 6 位代码或中文名称，从搜索结果一键添加。右侧全量看板包含所有自选，涨停置顶，可直接删除；左侧独立雷达显示急速拉升、放量跳水、首次封板、炸板，每只股票保留最近一次事件，120 秒变灰、300 秒移除，新事件刷新该股的雷达时钟并置顶。普通异动榜仍使用原有 420 秒时限。

均衡档自选急拉条件为 1 分钟窗口涨速至少 2%、量比就绪时至少 2 倍且当前累计主力净流入为正，按条件从不满足到满足触发；不受普通榜每分钟 10 条限额，也不占用普通名额。即使已在普通异动榜上，也能再次触发独立雷达。涨速窗口口径沿用现有实现，1 分钟实际跨度见 `window_1m_seconds`（旧 2 分钟兼容字段为 `window_seconds`）。自选告警在 Notifier 中优先发送并绕过普通 300 秒冷却，前端仍按事件 ID 去重；VIP Toast 保留 12 秒、金边强调并按多空保留红/绿方向色，在其他页签显示可点击的置顶横幅。声音仍需手动开启，爆发事件采用有界音频排队并优先提示 VIP。

`watchlist.json` 在后端根目录自动创建，仅保存代码数组，重启后恢复；使用临时文件、flush/fsync、原子替换，写入成功后才更新内存。文件内容损坏时拒绝覆盖并停止启动，应修复文件后重启。该文件和临时文件均不提交 Git，生产部署时保留它；这是单进程实例共享的一份自选列表，不是多用户账户配置。

接口均沿用 `API_TOKEN` 认证，写入还校验浏览器 Origin：

| 接口 | 行为 |
| --- | --- |
| `GET /api/watchlist` | 返回全部自选行情，缺失报价明确标记 |
| `POST /api/watchlist` | 请求 `{"code":"600036"}`，返回内存中的初始行情；重复添加幂等 |
| `DELETE /api/watchlist/{code}` | 移除自选，重复删除幂等 |
| `GET /api/stocks/search?q=招商` | 对代码或名称进行子串匹配，最多 30 条 |

添加和搜索均基于真实行情缓存与真实交易日历，校验成熟的 00/60 主板股票（包含 ST）；真实元数据不可用时拒绝新添加，不使用模拟种子伪造上市资格。已保存代码仍保留在看板中。WebSocket 每轮新增 `watchlist_anomalies` 和 `watchlist_leaders` 两个数组，管理操作也立即广播同步。

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

## 行业资料与收盘持久化

行业与概念优先采用新浪个股资料页（申万行业），保存到 company_sectors.json；批量证监会行业和概念成分股缓存 sector_cache.json 用于补充，冲突行业留空。两者独立后台低并发刷新；成功当日不重复抓取，失败保留旧缓存并按小时重试。不完整的板块不覆盖有效数据，源站缺少的标签不猜测填充。腾讯实时行情只关联内存映射，不等待资料请求。

market_snapshot.json 仅保存真实腾讯结果，盘中每分钟原子落盘，午休/收盘后再验证并保存一次对应时点的报价。休市展示保留 as_of/time，重启后恢复，不推进告警状态机；模拟或损坏缓存会被拒绝。上述缓存均不提交 Git。
