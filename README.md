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

- 第一栏使用后端 `OrderedDict` 顺序；120 秒变灰，满 420 秒移除。达到涨停价立即移出（以收到该股票成功报价的轮次为准）。临时缺股保留最后值和入榜时钟，`quote_missing=true` 标记待更新；旧值不参加当轮截面排名。
- 集合竞价允许成交量为零；09:15–09:30 未成交报价的成交量、成交额、换手和资金字段若为 `"-"`/空值，则按零处理。有成交量却缺失资金字段时仍拒绝，负成交量或无效价格/涨跌幅也拒绝。整包抓取仍检查总数、重复页和完整性；不使用个股 `f124` 的 30 秒 age 删除股票。
- `main_inflow=f62`、`super_inflow=f66`、`large_inflow=f72` 分别表示主力、超大单、大单净流入，单位元。`total_net_inflow=super_inflow+large_inflow`，`total_net_inflow_scope=super+large`，含义为“超大单＋大单合计”，不称为全市场/全口径资金净流入，也不把已包含超大单的主力再加一次。
- `seal_amount=price*buy_one_lots*100`，单位元；`seal_float_pct` 已是百分数。盘口缺失时封单金额可能为 null，页面显示“—”。集合竞价撮合价达到涨停价仍沿用现有封板状态机语义，不额外声称为已成交封板。
- 2 分钟涨速沿用 `100*(当前价-窗口最早价)/昨收价`；窗口不足两分钟可触发，`window_seconds` 给出实际跨度。此项未在本次任务内重定义。
- 事件具有独立 id；客户端有界去重，重连初始快照不回放旧事件。声音必须点击启用，不会自动突破浏览器播放限制。

## 自检

```powershell
.\.venv\Scripts\python.exe engine.py
.\.venv\Scripts\python.exe -m unittest -v test_backend
cd frontend
npm test
npm run build
```

若当前工作站虚拟环境 SSL 导致测试客户端不可用，可使用系统 Python 加载已安装依赖运行后端回归：

```powershell
python -X utf8 -c "import ssl,sys,runpy; sys.path.insert(0,r'.venv/Lib/site-packages'); sys.argv=['unittest','-v','test_backend']; runpy.run_module('unittest',run_name='__main__')"
```

`test_backend.py` 覆盖 09:27 静默报价、原状态机边界、缺失股票保留、涨停移出、资金合计、WS 连接断开/取消的任务回收，以及带 Token 的权重更新。旧《后端实现与自检报告》记录的是修改前状态，其源码及测试结果不能代表当前版本。
