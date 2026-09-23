# 服务器部署指南

目标：把 DeepQuery 部署到自己的服务器上，对外给一个链接 + 访问口令，访客打开就能提问。

公网部署和本机运行最大的区别是：**每一次提问都在花你的 API 额度，而访问链接的人你控制不了。**
所以下面每一步都围绕三件事：别被刷爆账单、访客之间互不影响、服务器本身不暴露多余的口子。

## 0. 前提

- 一台 Linux 服务器（1 核 2G 足够），已安装 Docker 与 compose 插件：`docker compose version` 能输出版本号
- 一个域名（推荐；没有也能先用 IP 临时访问，见第 4 步末尾）
- 安全组 / 防火墙放行 80 和 443

## 1. 先在模型服务商那里设好消费上限

这一步在服务器之外，但它是**最硬的一道防线**：应用里的所有限制都可能因为配置失误或 bug 失效，
而服务商后台的额度上限不会。

- 在 API 服务商或中转平台的后台设置每日 / 每月消费上限，或者只充值一笔固定额度
- 给在线演示**单独开一个 API Key**，不要和你跑评测用的 Key 共用——泄露了只吊销这一个

## 2. 拉代码并配置

```bash
git clone https://github.com/BlackMeovv/DeepQuery.git
cd DeepQuery
cp .env.example .env
vim .env
```

`.env` 里需要改的：

```
LLM_API_KEY=演示专用的 key
LLM_BASE_URL=你的接口地址/v1
LLM_MODEL=模型名

DEMO_ACCESS_CODE=给访客的访问口令
RATE_LIMIT_PER_MINUTE=6
DAILY_COST_LIMIT=1
TRUST_PROXY_HEADERS=true
GRAFANA_ADMIN_PASSWORD=换一个强密码
```

各项的作用：

| 配置 | 作用 |
|---|---|
| `DEMO_ACCESS_CODE` | 提问和记忆读写都要口令，前端会弹窗询问并记住。同时每个浏览器分到独立的访客 ID，**记忆按访客隔离**——访客看不到、也改不了别人的记忆 |
| `RATE_LIMIT_PER_MINUTE` | 每个访客每分钟最多提问次数，超出时界面提示"请求太频繁" |
| `DAILY_COST_LIMIT` | 全站每日模型花费上限（与 `LLM_PRICE_*` 同币种）。用完后新问题提示"今日额度已用完"，已经问过的问题仍能从缓存直接看到 |
| `TRUST_PROXY_HEADERS` | 在 nginx 后面时必须打开，否则所有访客会被当成同一个人一起限流。**只有应用端口不对公网开放时才安全**（compose 默认如此） |
| `AGENT_MAX_COST_PER_RUN` | 单次提问的花费上限（默认 0.05），已默认开启 |

还有两个建议：

- **模型选快的**（如 deepseek-chat）。推理模型一个问题要几十秒，演示时体验很差；演示库难度下准确率差别不大
- `LLM_PRICE_INPUT_PER_M` / `LLM_PRICE_OUTPUT_PER_M` 填成你所用模型的真实单价，否则每日上限按错误的价格计算

## 3. 启动

```bash
docker compose up -d --build app redis
curl http://127.0.0.1:8000/healthz
```

看到 `"ok":true,"protected":true` 即成功。首次构建约 3–5 分钟（包含前端构建）。

**数据不需要手动准备**：容器第一次启动时发现没有数据库文件，会自动生成演示库并存进 named volume，
之后重启、重建都沿用同一份。演示库是确定性生成的（固定随机种子），每台机器生成的内容完全一样，
所以文档和截图里的数字在你的服务器上也能复现。里面是什么见下一节。

compose 默认把所有端口都只绑定在 `127.0.0.1`。这很重要：**Docker 发布的端口会绕过 ufw 等主机防火墙**，
如果绑定在 `0.0.0.0`，即使 ufw 里没放行也会直接暴露在公网上。所以外部访问一律经过下一步的 nginx。

需要监控大盘时再加 `prometheus grafana`，并通过 SSH 隧道访问，不要对外开放：

```bash
docker compose up -d prometheus grafana
ssh -L 3000:127.0.0.1:3000 你的用户@服务器IP
```

然后在本机浏览器打开 `http://localhost:3000`。

## 4. nginx + HTTPS

新建 `/etc/nginx/conf.d/deepquery.conf`：

```nginx
# 访问口令在 URL 参数里，日志只记路径不记参数
log_format dq_noargs '$remote_addr [$time_local] "$request_method $uri" $status $body_bytes_sent';

server {
    listen 80;
    server_name dq.example.com;
    access_log /var/log/nginx/deepquery.access.log dq_noargs;

    # 监控指标只给内部的 Prometheus 抓取
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
```

然后：

```bash
nginx -t
systemctl reload nginx
certbot --nginx -d dq.example.com
```

几个配置的原因：

- `X-Real-IP $remote_addr`：由 nginx 写入真实来源地址，覆盖访客自己伪造的同名请求头，限流才能按人计算
- `proxy_buffering off` 和较长的 `proxy_read_timeout`：流式输出必需，否则回答不会逐字出现
- HTTPS：访问口令在 URL 里，不加密的话途经的网络都能看到

**暂时没有域名**：可以在 `.env` 里设 `APP_BIND=0.0.0.0`、把 `TRUST_PROXY_HEADERS` 改回 `false`，
重新 `docker compose up -d app` 后用 `http://服务器IP:8000` 访问。这种方式没有 HTTPS，只适合短时间自己测试。

## 5. 访客看到的数据

演示库是一家**虚构电商** 2024 年 1 月至 2025 年 6 月的经营数据，全部为程序生成，不含任何真实信息：

| 表 | 内容 |
|---|---|
| `customers` | 240 位客户：姓名、城市（10 个）、注册日期、会员等级 |
| `categories` / `products` | 6 个品类、36 个商品：售价与成本 |
| `orders` | 1500 笔订单：下单客户、日期、状态（已完成 / 已发货 / 待处理 / 已取消） |
| `order_items` | 订单明细：商品、数量、成交单价 |
| `payments` | 支付流水：金额、支付方式、支付时间 |

网页首页会显示一段数据说明和几个示例问题，访客不用看表结构也知道能问什么。
用自己的数据库时，在 `.env` 里写一句 `DATASET_NOTE=这份数据是……` 替换首页的说明。

**公网演示请一直用演示库**。换成自己的数据（`DB_PATH` 指向别的 SQLite 文件，或 MySQL / PostgreSQL 的只读账号）
意味着拿到链接和口令的人都能查询它，只在内网或自己使用时这样做。

### 给访客的试用建议

把链接和口令发给别人时，可以附上这几个问题，基本覆盖了主要能力：

| 试试问 | 能看到什么 |
|---|---|
| 各品类的成交金额分别是多少？ | 常规查询：展开"运行过程"能看到生成的 SQL 和执行结果 |
| 哪个客户最好？ | "最好"没有定义，Agent 不会自己猜，而是先问按消费金额、订单数还是会员等级；选了之后可以勾选"记住"，下次同样的说法直接按这个口径算 |
| 各城市的退货率是多少？ | 库里没有退货记录，Agent 会说明缺什么，并给出能回答的相近问题（如取消订单占比），点一下就能改问 |
| 把"最好"的问题再问一遍 | 刚才记住的口径生效，这次不再反问 |
| 勾选"生成图表"再问一个趋势问题 | 画图代码在沙箱里执行，返回图片 |
| 重复问同一个问题 | 命中缓存，零消耗秒回 |

## 6. 上线前自检

- [ ] 服务商后台已设消费上限，演示用的是单独的 API Key
- [ ] 浏览器打开网站会弹出口令框；输错口令无法提问
- [ ] 连续快速提问，第 7 次左右出现"请求太频繁"
- [ ] 用两个不同的浏览器（或一个开无痕窗口）分别添加记忆，互相看不到
- [ ] 从本机执行 `curl http://服务器IP:8000/healthz` 连不上（说明应用端口没有直接暴露）
- [ ] `curl https://dq.example.com/metrics` 返回 403
- [ ] 回答是逐字出现的，而不是等很久一次性出来

## 7. 日常运维

```bash
docker compose logs -f app
git pull && docker compose up -d --build app
docker compose down
```

依次是：看日志、更新代码后重建、整体下线。演示库、记忆和图表在 named volume `app-data` 里，重建不会丢。

口令外泄时：改 `.env` 里的 `DEMO_ACCESS_CODE`，再执行 `docker compose up -d app`，旧口令立即失效。

## 8. 防护一览

| 风险 | 措施 |
|---|---|
| 链接被转发、额度被刷光 | 访问口令 + 每访客限流 + 全站每日花费上限 + 单次提问预算，最外层是服务商后台的消费上限 |
| 访客互相干扰或写入恶意"记忆" | 记忆按浏览器隔离，每个访客最多 50 条 |
| 模型写出修改数据的 SQL | 语法树守卫只放行单条 SELECT；数据库以只读方式打开；演示库随时可重新生成 |
| 超大查询结果撑爆内存 | 结果行数上限 + 单个值 1MB 上限 |
| 模型生成的画图代码 | 在容器内以带资源限额的子进程执行，产物只接受普通 PNG 文件，不跟随符号链接 |
| 端口意外暴露 | 所有容器端口只绑定本机，对外只开 nginx 的 80 / 443；`/metrics` 禁止外部访问 |
| 口令在网络上被截获 | HTTPS；nginx 日志不记录 URL 参数 |
| 只想公开部分数据 | `.env` 中 `ALLOWED_TABLES=orders,products`，模型看不到也查不了其余的表 |
