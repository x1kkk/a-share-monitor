# 主板雷达前端

Vue 3 + Vite + TailwindCSS 4；无外部字体、图标库或 CDN 运行依赖。Node.js 20.19+ 或 22.12+。npm 缓存限于工程内 `.npm-cache/`；Vite 使用原生配置加载，避免 Windows 受限环境中配置打包枚举祖先目录失败。

```powershell
cd frontend
npm ci
npm run test
npm run dev
```

开发地址为 http://127.0.0.1:5173，Vite 将 `/api`、`/health`、`/ws` 代理到 `127.0.0.1:8000`。后端应将开发地址纳入 `CORS_ORIGINS`。

```powershell
npm run build
```

生成的 `dist/` 可以直接由 FastAPI 或 Nginx 托管，根目录 `nginx.conf` 配置 API 与 WebSocket 反向代理。所有请求使用当前页面同源地址；正式环境通过 HTTPS 访问时自动使用 WSS。请为后端配置允许的正式前端 Origin。`npm run preview` 仅用于查看静态构建，正式联调使用 FastAPI/Nginx 托管或 Vite 开发服务。

后端配置 `API_TOKEN` 时，在页头“连接设置”中填写令牌并重新连接。令牌通过 WebSocket 首消息 `{ "token": "..." }`、HTTP `Authorization: Bearer ...` 传递，仅保存于当前标签页 `sessionStorage`，不会写入源码、构建环境变量或 URL。未启用认证时留空。

## 行为约定

- 行情只来自 `/ws` 的 `type: "snapshot"`；严格保留后端数组顺序，不在前端排序或删除过期记录。`status: "expired"` 由后端决定，本地只推进显示秒数。`quote_missing` 明确显示报价待更新。
- 断线保留最后快照并显示警示；指数退避最多 30 秒重连；40 秒没有快照或心跳则重连。1008 拒绝连接会停止重试并提示检查令牌/Origin。
- 首次连接与每次重连的第一条快照只同步状态，不回放告警。其后根据 `event.id` 去重，最多保留 2000 个 ID。Toast 自动保留 7 秒，可手动关闭。
- 声音需要用户点击开启，并播放测试音。异动、首次封板、炸板使用三组不同频率。连续事件的音效最多排队约 1.6 秒，超出部分合并为已排队的声音，避免积压播报；通知卡片仍同步处理。重新打开页面需要再次授权；若浏览器暂停 AudioContext，请关闭再开启声音。声音关闭期间的事件不补播。
- 权重拖动 300ms 防抖，HTTP 保存串行，期间的新编辑合并为最后一份。保存失败保留草稿并可重试；后端归一化百分比，不能全部为零。
- `main_inflow` 为主力净流入，`super_inflow` 为超大单净流入，`large_inflow` 为大单净流入；`total_net_inflow` 显示为“超大单＋大单合计”，不在主力上重复叠加超大单。金额单位原始为元，自动转换万/亿；缺失值显示 `—`。
- 宽屏默认同屏双栏，也可切到单栏；窄屏显示页签。涨红跌绿同时显示数值符号，避免仅凭颜色识别。支持系统减少动态效果偏好与键盘焦点。

`npm test` 检查金额单位、事件去重、重连抑制、数组顺序、倒计时不移除股票与权重保存并发顺序。
