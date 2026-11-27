# 贪吃蛇 / Snake Club

独立网页游戏，无前端依赖、构建步骤、账号或服务器存档。支持方向键 / WASD、滑动、触屏方向键、空格暂停、自动离开暂停、三档速度、分难度本机最高分。更改速度会重置本局；撞墙或碰到自身结束。

## 本地运行和检查

从仓库根目录执行：

```sh
python -m http.server 8093 --bind 127.0.0.1 --directory apps/snake
node --test apps/snake/engine.test.mjs
python -m unittest discover -s apps/snake -p 'test_server.py' -v
```

打开 `http://127.0.0.1:8093/`。最高分只保存在当前浏览器，禁用存储时仍可游玩。

## 当前生产部署

生产服务器：`ubuntu@106.55.251.11`；SSH 密钥从本机受保护文件加载，不随源码上传。

- 独立端口：TCP `8091`，访问 `http://106.55.251.11:8091/`。
- 目录：`/opt/apps/snake-game/releases/<commit>/`；`current` 软链接指向发布版本。
- 服务：`snake-game.service`，使用独立动态低权限用户，systemd 自启和故障重启。
- 复用已有 `/opt/apps/a-share-monitor/.venv/bin/python` 中的 Uvicorn；不新增依赖，不修改行情服务。
- 仅上传 `index.html`、`style.css`、`app.mjs`、`engine.mjs`、`server.py`；服务仅响应固定静态路径和 `/health`，不暴露仓库或测试文件。
- 防火墙和云安全组仅需新增 `8091/tcp` 入站规则。发布后必须从服务器本机及外网检查页面、模块 MIME 和 `/health`。

部署前确认端口未占用，将上述五个文件放入一个新的发布目录，设置目录 `0755`、文件 `0644`。保存现有 `current` 目标后，切换软链接，将 `snake-game.service` 安装到 `/etc/systemd/system/`，执行：

```sh
sudo systemd-analyze verify /etc/systemd/system/snake-game.service
sudo systemctl daemon-reload
sudo systemctl enable --now snake-game.service
curl -fsS http://127.0.0.1:8091/health
```

后续发布切换 `current` 后执行 `sudo systemctl restart snake-game.service`。若健康检查失败，切回上一次 `current` 目标并重启；首次部署失败则停止该独立服务。旧发布目录保留以供回滚。日志：`sudo journalctl -u snake-game.service --no-pager -n 30`。
