# 服务器部署指南

目标：把 DeepQuery 部署到自己的服务器上，对外给一个链接 + 访问口令，访客打开就能提问。

公网部署和本机运行最大的区别是：**每一次提问都在花你的 API 额度，而访问链接的人你控制不了。**
所以下面每一步都围绕三件事：别被刷爆账单、访客之间互不影响、服务器本身不暴露多余的口子。

想从一台全新服务器（还没装 Docker）一步步执行下来，用 [SERVER-RUNBOOK.md](SERVER-RUNBOOK.md)：
每步都有命令和通过标准，也可以直接交给 Agent 执行。本文解释每一步为什么这样做。

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
| `MAX_CONCURRENT_RUNS` | 全站同时运行的提问数（默认 4），超出时提示稍后再试，防止小内存服务器被并发拖垮 |

还有两个建议：

- **模型选快的**（如 deepseek-chat）。推理模型一个问题要几十秒，演示时体验很差；演示库难度下准确率差别不大
- `LLM_PRICE_INPUT_PER_M` / `LLM_PRICE_OUTPUT_PER_M` 填成你所用模型的真实单价，否则每日上限按错误的价格计算

## 3. 启动

```bash
docker compose up -d --build app redis
curl http://127.0.0.1:8000/healthz
```

看到 `"ok":true,"protected":true` 即成功。首次构建约 3–5 分钟（包含前端构建）。

**数据不需要手动准备**：容器第一次启动时发现 `DB_PATH` 指向的库不存在，会自动生成（演示库）
或下载导入（Olist 真实数据）并存进 named volume，之后重启、重建都沿用同一份。用哪份数据见下一节。

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

## 5. 选择数据

内置两份数据，改 `.env` 里的 `DB_PATH` 即可切换。首页的数据说明、示例问题和业务口径会跟着一起换。

| | Olist 巴西电商（推荐公网演示） | 演示库（默认） |
|---|---|---|
| 数据 | **真实订单**（已匿名化），2016–2018 年 | 程序生成的虚构电商，2024–2025 年 |
| 规模 | 约 10 万订单、9.6 万客户、3 千卖家、3.3 万商品、10 万条评价 | 1500 订单、240 客户、36 个商品 |
| 内容 | 订单、明细、支付、物流时间、评分，品类和州带中文名 | 订单、明细、支付、会员等级 |
| 用途 | 让访客体验真实规模的数据 | 评测集基于它，README 里的准确率在这份数据上测得 |
| `DB_PATH` | `data/olist/olist.sqlite` | `data/demo/ecommerce.sqlite` |

### 换成 Olist 真实数据

在 `.env` 里写：

```
DB_PATH=data/olist/olist.sqlite
```

然后重启：

```bash
docker compose up -d app
docker compose logs -f app
```

第一次启动会从 Kaggle 下载约 45MB 的压缩包并导入（1 分钟左右，日志里能看到进度），生成的库约 115MB。
Kaggle 的文件存在谷歌云存储上，**服务器在国内时通常下载不了**（日志里会提示下载失败，网站照常启动但查询会报错）。
这时在自己电脑上打开 [Kaggle 数据集页面](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)
下载压缩包（通常叫 `archive.zip`），传到服务器的项目目录后执行：

```bash
docker compose run --rm -v "$PWD/archive.zip:/tmp/olist.zip:ro" app uv run python -m deepquery.olist_data --src /tmp/olist.zip
docker compose restart app
```

Olist 数据的许可是 CC BY-NC-SA 4.0：可以用于非商业的作品展示，需要注明出处。首页已经自动显示了数据来源。

Olist 的几张表：

| 表 | 内容 |
|---|---|
| `orders` | 订单：状态、下单 / 付款 / 发货 / 签收时间、承诺送达日期 |
| `order_items` | 订单明细：商品、卖家、成交价、运费 |
| `payments` | 支付：方式（信用卡 / 银行票据 / 代金券…）、分期期数、金额 |
| `reviews` | 买家评价：1–5 星评分和评论（葡萄牙语） |
| `customers` / `sellers` | 买家和卖家所在的城市、州 |
| `products` / `categories` | 商品重量尺寸；73 个品类，带中文名 |
| `states` | 巴西 27 个州的中文名和所属大区 |

### 用自己的数据

`DB_PATH` 指向别的 SQLite 文件，或 MySQL / PostgreSQL 的只读账号，再在 `.env` 里写一句
`DATASET_NOTE=这份数据是……` 作为首页说明。**不要把公司的真实业务数据放在公网演示上**：
拿到链接和口令的人都能查询它。

### 给访客的试用建议

把链接和口令发给别人时，可以附上这几个问题（以 Olist 为例，首页也列出了前四个）：

| 试试问 | 能看到什么 |
|---|---|
| 销售额最高的 5 个品类是哪些？ | 常规查询：展开"运行过程"能看到生成的 SQL；品类显示中文名 |
| 延迟送达的订单，评分会比准时的低多少？ | 真实数据里的洞察：延迟送达平均 2.3 星，准时的 4.3 星 |
| 哪个卖家最好？ | "最好"没有定义，Agent 不会自己猜，输入框会变成选项面板，问按销售额、订单数还是评分来排 |
| 各品类的退货率是多少？ | 数据里没有退货记录，Agent 会说明缺什么，并给出能回答的相近问题，点一下就能改问 |
| 有多少客户买过两次以上？ | 业务口径生效：客户要按 `unique_id` 去重，而不是数订单级的客户 ID |
| 勾选"生成图表"，问"2017 年每个月的订单量" | 画图代码在沙箱里执行，返回趋势图 |
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

依次是：看日志、更新代码后重建、整体下线。演示库、记忆、图表和运行记录都在 named volume `app-data` 里，重建不会丢。

看最近 7 天的运行概况（提问次数、延迟、花费、好评差评，以及最近的差评）：

```bash
docker compose exec app uv run deepquery runs
```

把差评导出成待标注的评测用例，逐条补上正确的 SQL 后并入评测集：

```bash
docker compose exec app uv run deepquery feedback-export --out data/feedback-todo.jsonl
docker compose cp app:/app/data/feedback-todo.jsonl .
```

运行记录存问题、SQL、回答文本和用量，不单独存查询结果（回答被降级为结果预览时，预览会随回答一起存下）；
超过 `RUN_LOG_KEEP` 条后自动删除最旧的。
公网演示时访客的提问会被记下，如需告知访客，在页面说明或 `DATASET_NOTE` 里写明即可。

口令外泄时：改 `.env` 里的 `DEMO_ACCESS_CODE`，再执行 `docker compose up -d app`，旧口令立即失效。

## 8. 防护一览

| 风险 | 措施 |
|---|---|
| 链接被转发、额度被刷光 | 访问口令 + 每访客限流 + 全站每日花费上限 + 单次提问预算 + 全站并发上限，最外层是服务商后台的消费上限 |
| 提问后立刻关掉页面，反复刷 | 浏览器断开时正在运行的提问立即停止，不再调用模型；已花掉的部分（包括失败和中途取消的）照样计入每日上限 |
| 每位新访客都为示例问题重新付费 | 结果缓存按记忆内容区分而不是按访客：没有私有记忆的访客共享同一份答案 |
| 访客互相干扰或写入恶意"记忆" | 记忆按浏览器隔离，每个访客最多 50 条 |
| 模型写出修改数据的 SQL | 语法树守卫只放行单条 SELECT；数据库以只读方式打开；演示库随时可重新生成 |
| 超大查询结果撑爆内存 | 结果行数上限 + 单个值 1MB 上限 |
| 模型生成的画图代码 | 在容器内以带资源限额的子进程执行，并换成独立的无权限用户：读不到服务进程环境变量里的 API Key 和数据目录，进程数有上限，结束后清理干净；产物只接受普通 PNG 文件，不跟随符号链接 |
| 端口意外暴露 | 所有容器端口只绑定本机，对外只开 nginx 的 80 / 443；`/metrics` 禁止外部访问 |
| 口令在网络上被截获 | HTTPS；nginx 日志不记录 URL 参数 |
| 深度分析一次调很多次模型 | 步数上限（默认先拆 4 步、总共最多 6 步，下钻只一轮），整次分析共用一个预算（单次提问的 4 倍）；各步并行、占一个并发名额；花费同样计入每日上限 |
| 事后查不清谁问了什么 | 每次提问留一条运行记录（访客 ID、问题、SQL、状态、耗时、花费），`deepquery runs` 查看概况 |
| 只想公开部分数据 | `.env` 中 `ALLOWED_TABLES=orders,products`，模型看不到也查不了其余的表 |
