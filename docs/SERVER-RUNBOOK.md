# 服务器部署执行清单

给执行部署的人或 Agent 用：从一台全新的 Linux 服务器开始，到网站可以发给别人试用。
按顺序执行，每一步都有**通过标准**，不通过就先看该步的"没通过时"，仍不行就停下来把输出交给用户，不要跳步。
每一步为什么这样做，见 [DEPLOY.md](DEPLOY.md)。

## 执行约定

- 除非标明"本机"，所有命令都在服务器上执行（通过 `ssh <用户>@<服务器IP>` 登录后）。
- 尖括号 `<…>` 是需要替换的值，来自第 0 步向用户确认的信息；没有的信息不要编造，先问用户。
- API Key、访问口令只写进服务器上的 `.env`，不要提交到仓库，也不要在输出里完整打印。
- 不修改仓库里的代码；所有配置都在服务器上的 `.env` 里改。

## 0. 开工前向用户确认

| 信息 | 说明 |
|---|---|
| 服务器 IP、SSH 用户 | 能 ssh 登录，并且有 sudo 权限 |
| 服务器地域 | 中国大陆，还是香港 / 海外。大陆服务器需要镜像加速，数据也要手动上传 |
| 域名（可选） | 已解析到服务器 IP。**大陆服务器用域名访问必须先完成 ICP 备案**，否则会被云厂商拦截 |
| 证书邮箱 | 有域名时申请 HTTPS 证书用 |
| 模型 API | `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`，以及该模型每百万 token 的输入 / 输出单价 |
| 访问口令 | 发给访客的口令 |
| 数据 | Olist 真实数据（推荐）或演示库 |

模型 API 推荐 DeepSeek（国内可直连、OpenAI 兼容）：`LLM_BASE_URL=https://api.deepseek.com/v1`，`LLM_MODEL=deepseek-chat`。
让用户先在服务商后台**设好消费上限**，并给演示单独建一个 Key。

## 1. 检查服务器

```bash
cat /etc/os-release
uname -m
free -h
df -h /
docker --version
docker compose version
```

**通过标准**：Ubuntu 20.04+ 或 Debian 11+（其他发行版把下面的 apt 命令换成对应的包管理器）；内存 ≥ 1G；根分区剩余 ≥ 10G。
如果 Docker ≥ 24 且 `docker compose version` 有输出，跳过第 3 步。

再检查外网连通性，结果记下来，第 3、5、6 步要用：

```bash
curl -sS -m 10 -o /dev/null -w "docker hub %{http_code}\n" https://registry-1.docker.io/v2/
curl -sS -m 10 -o /dev/null -w "ghcr %{http_code}\n" https://ghcr.io/v2/
curl -sS -m 10 -o /dev/null -w "kaggle %{http_code}\n" https://storage.googleapis.com/
curl -sS -m 10 -o /dev/null -w "llm api %{http_code}\n" <LLM_BASE_URL>/models
```

输出三位数状态码（200 / 401 / 404 等）说明能连通；超时或 `000` 说明连不上。模型 API 必须能连通，否则停下来告诉用户。

## 2. 基础设置

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates ufw
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
sudo ufw status
```

**通过标准**：`ufw status` 显示 active，并放行了 OpenSSH、80、443。
SSH 不是 22 端口时，把 `OpenSSH` 换成实际端口（如 `2222/tcp`），**先放行再 enable**，否则会把自己锁在外面。
另外提醒用户去云厂商控制台的**安全组**里也放行 22、80、443。

内存小于 3G 时加 2G 交换空间，否则构建镜像时可能因内存不足失败：

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h
```

**通过标准**：`free -h` 的 Swap 一行显示 2.0Gi。

## 3. 安装 Docker

香港 / 海外服务器：

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
```

中国大陆服务器：

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh --mirror Aliyun
```

然后让当前用户免 sudo 使用 Docker，并**退出重新登录**一次让它生效：

```bash
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
exit
```

重新 ssh 登录后验证：

```bash
docker --version
docker compose version
docker run --rm hello-world
```

**通过标准**：最后一条输出里有 `Hello from Docker!`。

**没通过时**：
- `get.docker.com` 下载不了：改用系统源安装 `sudo apt-get install -y docker.io docker-compose-v2`，再继续上面的验证。
- `hello-world` 拉取超时（大陆服务器常见，Docker Hub 连不上）：请用户到云厂商控制台拿**镜像加速地址**
  （阿里云：容器镜像服务 → 镜像工具 → 镜像加速器），然后：

```bash
sudo mkdir -p /etc/docker
echo '{"registry-mirrors": ["<镜像加速地址>"]}' | sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
docker run --rm hello-world
```

## 4. 拉代码并配置

```bash
cd ~
git clone https://github.com/BlackMeovv/DeepQuery.git
cd DeepQuery
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，把下面这些行改成（或追加为）对应的值，已有的同名行要替换而不是重复：

```
LLM_API_KEY=<用户提供>
LLM_BASE_URL=<用户提供>
LLM_MODEL=<用户提供>
LLM_PRICE_INPUT_PER_M=<该模型输入单价>
LLM_PRICE_OUTPUT_PER_M=<该模型输出单价>
DEMO_ACCESS_CODE=<访问口令>
RATE_LIMIT_PER_MINUTE=6
DAILY_COST_LIMIT=1
TRUST_PROXY_HEADERS=true
GRAFANA_ADMIN_PASSWORD=<随机生成的强密码>
DB_PATH=data/olist/olist.sqlite
```

- 用演示库时，`DB_PATH` 保持 `data/demo/ecommerce.sqlite`。
- 没有域名、准备直接用 `http://服务器IP:8000` 访问时：`TRUST_PROXY_HEADERS=false`，并追加 `APP_BIND=0.0.0.0`。
- 第 1 步里 **ghcr 连不上**（大陆常见）：追加下面两行。镜像代理地址不保证长期可用，先用 `docker pull` 试，拉不下来就改用第 5 步末尾的"本机构建再上传"。

```
UV_IMAGE=ghcr.nju.edu.cn/astral-sh/uv:python3.11-bookworm-slim
NPM_REGISTRY=https://registry.npmmirror.com
```

```bash
docker pull ghcr.nju.edu.cn/astral-sh/uv:python3.11-bookworm-slim
```

**通过标准**：下面这条命令列出的值都正确（不打印 Key 本身）：

```bash
grep -E '^(LLM_BASE_URL|LLM_MODEL|DB_PATH|RATE_LIMIT_PER_MINUTE|DAILY_COST_LIMIT|TRUST_PROXY_HEADERS|APP_BIND|UV_IMAGE)=' .env
grep -c '^LLM_API_KEY=sk-xxxxxxxx' .env
grep -c '^DEMO_ACCESS_CODE=.\+' .env
```

倒数第二条应输出 `0`（示例 Key 已被替换），最后一条应输出 `1`。

## 5. 构建并启动

```bash
docker compose up -d --build app redis
docker compose ps
docker compose logs --tail 50 app
```

首次构建 3–10 分钟。用 Olist 数据时，第一次启动还会下载约 45MB 并导入，日志里能看到进度。

```bash
curl -s http://127.0.0.1:8000/healthz
docker compose exec app uv run deepquery check-api
```

**通过标准**：
- `healthz` 返回 `"ok":true`、`"protected":true`，`db` 是 `olist.sqlite`（或 `ecommerce.sqlite`），`dataset_note` 不为空
- `check-api` 的 4 项检查通过

再问一个真实问题（把口令换进去）：

```bash
curl -sN -G http://127.0.0.1:8000/api/ask --data-urlencode "question=一共有多少笔订单？" --data-urlencode "code=<访问口令>" | tail -n 3
```

**通过标准**：最后一个 `final` 事件里 `"status": "ok"`。

**没通过时**：
- 日志里有"生成数据集 olist 失败"：服务器下载不了 Kaggle，按第 6 步手动导入。
- 构建卡在 `FROM ghcr.io/...` 或 `npm ci`：按第 4 步设置 `UV_IMAGE` / `NPM_REGISTRY` 后重新执行本步；仍不行就本机构建再上传（见下）。
- 构建中途退出、日志有 `exit code: 137`：内存不足，回第 2 步加交换空间。
- `check-api` 报 401 / 403：Key 或 Base URL 不对；报 402 或余额不足：让用户充值。
- 网页提问时出现"连接中断"：先看 `docker compose logs --tail 100 app` 有没有报错堆栈；没有报错的话，
  多半是中间的代理按空闲超时断开了连接。服务每 10 秒发一次心跳，经过云负载均衡或 CDN 时把它们的空闲超时设到 60 秒以上。

**本机构建再上传**（服务器拉不到基础镜像时）。在用户自己的电脑上（**本机**，需要已装 Docker、网络通畅）：

```bash
git clone https://github.com/BlackMeovv/DeepQuery.git
cd DeepQuery
docker buildx build --platform linux/amd64 -t deepquery-app --load .
docker save deepquery-app | gzip > deepquery-app.tar.gz
scp deepquery-app.tar.gz <用户>@<服务器IP>:~/
```

服务器是 ARM（第 1 步 `uname -m` 输出 `aarch64`）时，把 `linux/amd64` 换成 `linux/arm64`。然后在服务器上：

```bash
gunzip -c ~/deepquery-app.tar.gz | docker load
cd ~/DeepQuery
docker compose up -d --no-build app redis
```

## 6. 手动导入 Olist 数据（第 5 步下载失败时）

在**本机**打开 https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce 下载压缩包（通常叫 `archive.zip`），上传到服务器：

```bash
scp archive.zip <用户>@<服务器IP>:~/DeepQuery/
```

在服务器上导入并重启：

```bash
cd ~/DeepQuery
docker compose run --rm -v "$PWD/archive.zip:/tmp/olist.zip:ro" app uv run python -m deepquery.olist_data --src /tmp/olist.zip
docker compose restart app
curl -s http://127.0.0.1:8000/healthz
rm archive.zip
```

**通过标准**：`healthz` 里 `db` 是 `olist.sqlite`，`dataset_source` 包含 `Olist`。

## 7. 域名与 HTTPS（有域名时）

```bash
sudo apt-get install -y nginx certbot python3-certbot-nginx
sudo tee /etc/nginx/conf.d/deepquery.conf > /dev/null <<'EOF'
log_format dq_noargs '$remote_addr [$time_local] "$request_method $uri" $status $body_bytes_sent';

server {
    listen 80;
    server_name <域名>;
    access_log /var/log/nginx/deepquery.access.log dq_noargs;

    location = /metrics { deny all; }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
}
EOF
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d <域名> -m <证书邮箱> --agree-tos --non-interactive --redirect
```

**通过标准**：

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://<域名>/healthz
curl -s -o /dev/null -w "%{http_code}\n" https://<域名>/metrics
```

依次输出 `200` 和 `403`。

**没有域名时**：确认第 4 步已设 `APP_BIND=0.0.0.0` 和 `TRUST_PROXY_HEADERS=false`，然后：

```bash
docker compose up -d app
sudo ufw allow 8000/tcp
```

并提醒用户在安全组放行 8000。访问地址是 `http://<服务器IP>:8000`。这种方式没有 HTTPS，只适合临时使用。

## 8. 验收

在浏览器里逐项确认，全部通过后把地址和口令交给用户：

- [ ] 打开网站会弹出口令框；输错口令不能提问
- [ ] 首页显示数据说明（Olist 时还有数据来源）和 4 个示例问题
- [ ] 点第 1 个示例：回答逐字出现，展开"运行过程"能看到 SQL
- [ ] 点"哪个卖家最好？"：输入框变成选项面板
- [ ] 连续快速提问 7 次左右，出现"请求太频繁"
- [ ] 有 nginx 时，从本机执行 `curl -m 5 http://<服务器IP>:8000/healthz` 连不上
- [ ] 服务商后台已设消费上限

## 9. 日常运维

更新到最新代码：

```bash
cd ~/DeepQuery
git pull
docker compose up -d --build app
```

查看日志、重启、下线：

```bash
docker compose logs -f app
docker compose restart app
docker compose down
```

数据、记忆和图表都在 named volume `app-data` 里，重建和重启都不会丢。换口令：改 `.env` 里的 `DEMO_ACCESS_CODE` 后执行 `docker compose up -d app`。

## 10. 跑评测（在本机做，不在服务器上）

评测会调用几百次模型，建议在用户自己的电脑上跑，结果文件直接留在仓库里。本机需要 [uv](https://docs.astral.sh/uv/) 和能访问 Kaggle 的网络：

```bash
cd DeepQuery
make install
cp .env.example .env
make olist-db
make olist-gold
make olist-eval LABEL=baseline
```

`.env` 里只需要填模型的三项。`make olist-gold` 是离线自检，必须 100%；`make olist-eval` 跑 96 题 × 3 次，
结果写到 `eval/results/`，把生成的 json 文件交给用户或提交到仓库。
