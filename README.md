# Stock Agent Assistant

### 开源的 A 股 AI 研判引擎

__9 位 AI 分析师，一套可核对的数据底座，把「今天能不能做」变成一条可以追溯的证据链。__

__四条命令跑起来 ↓__ · __先看 API 一览 →__ · __提个 Issue →__

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue.svg)
![Flask](https://img.shields.io/badge/Web-Flask-green.svg)
![Tests](https://img.shields.io/badge/Tests-pytest%20%C2%B7%2070%20files-brightgreen.svg)
![Data](https://img.shields.io/badge/Data-Tushare-orange.svg)

---

## 这是什么

Stock Agent Assistant 是一个跑在本机的 **A 股研究分析服务**：一端是行情、资金、财报、板块、新闻等原始数据，另一端是九位各管一摊的 AI 分析师。你问一只票，他们各自取数、各自发言、互相辩论，最后由主分析师给出裁决——每一句结论都挂着「依据」按钮，点开能看到这句话是从哪几个数字推出来的。

它不预测涨跌，也不替你做买卖决定。它做的是把一个散户平时**没时间核对、没渠道聚合**的证据（量价、资金、两融、ETF 份额、板块轮动、公告新闻），整理成同一口径下的判断，并诚实地告诉你「哪些数据今天还没出来」「这句话为什么可能是错的」。

```
数据层  Tushare · 百度财经 · 公告披露        ←  market_tape 统一口径 + 日频缓存
市场层  钱有没有进来 · 国家队 · 情绪阶段      ←  Mz 常态偏离 · 冰点→退潮阶段机
板块层  在攻 / 在出双榜 · 板块归属            ←  量价 + 资金同向分桶
个股层  6 维度分析师 → 看多/看空辩论 → 裁决   ←  LangGraph 流式 · 每句带依据
个人层  持仓 · 想买 · 提醒 · 复盘            ←  X-User-Id 隔离 · 本机存储
```

## 四条命令跑起来

Python 3.10+。数据来自 Tushare（需注册拿 token，积分制免费档够用），模型走任何 OpenAI 兼容接口（DeepSeek、通义、Kimi、本地 Ollama 都行）。

__1. 克隆并安装__

```bash
git clone https://github.com/<你的用户名>/stock_agent_assitant.git
cd stock_agent_assitant
pip install -r requirements.txt
```

__2. 写两个密钥__

在项目根目录建 `.env`（该文件不进 git）：

```dotenv
TUSHARE_TOKEN=去 tushare.pro 注册领取
LLM_API_KEY=你的模型密钥          # 也认 DEEPSEEK_API_KEY
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-flash
```

__3. 启动__

```bash
python -m app.main
# 股票查询服务: http://localhost:8090
```

__4. 问第一个问题__

```bash
curl "http://localhost:8090/api/v1/stocks/002428.SZ/dimensions/market/detail"
```

没有界面也能用——所有能力都是 HTTP API。配套的桌面客户端（研判群聊、持仓、行情页）是另一个仓库，凡是指向 `:8090` 的页面都能直接接上这套服务。

<details>
<summary>全部环境变量（点开）</summary>

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `TUSHARE_TOKEN` | Tushare 数据接口 token | 必填 |
| `LLM_API_KEY` / `DEEPSEEK_API_KEY` | 模型密钥（OpenAI 兼容） | 必填 |
| `LLM_BASE_URL` | 模型接口地址 | `https://api.deepseek.com/v1` |
| `LLM_MODEL` | 模型名 | `deepseek-flash` |
| `PORT` | 服务端口 | `8090` |
| `STOCK_DATA_DIR` | 数据库/缓存目录（打包客户端用） | 项目内 `data/` |
| `WECOM_WEBHOOK_URL` | 企业微信通知 webhook | 空 |
| `ENABLE_PRICE_ANCHORS` | 估值锚点价格（涉咨询资质，默认关） | 关 |

</details>

### 跑起来之后

九位分析师是按固定议程工作的，一次完整的研判像一场研究例会：

| 环节 | 发生了什么 | 接口 |
| --- | --- | --- |
| __各维度发言__ | 资金流/大盘/板块/新闻/财报/基本面六位分析师依次取数、给结论 | `dimensions/{维度}/stream` |
| __辩论__ | 看多、看空各说一轮，论据须通过对方核对 | `bull/stream` · `bear/stream` |
| __裁决__ | 主分析师综合各方，给出方向、得分、「如果我错了」的退出条件 | `chief/stream` |
| __追问__ | 对任何一位继续提问，@谁谁答 | `dimensions/{维度}/reply` |
| __留痕__ | 每句结论挂依据按钮，判断过期会标旧 | `*/detail` |

## 为什么选它

- __先取数，后说话。__ 分析师不凭训练记忆聊股票：每个维度先拉真实数据（资金流、两融、财报、公告），结论从数字里推出来，依据可点开核对。
- __多智能体互相制衡。__ 不是一个大模型包打天下：六维度各管一摊，多空两边必须过对方核对，主分析师只做裁决——错一次就在复盘里留一次记录。
- __市场层有自己的「物理学」。__ 资金用 Mz（对数常态偏离）度量，情绪有冰点→修复→发酵→高潮→退潮的阶段机，国家队用宽基 ETF 份额 T+1 监测——口径写死在代码里，不随模型心情漂移。
- __对数据诚实。__ 份额没更新就说「判不了出手日」，证据不够就说「凑不齐」，判断过期就标「已过期」。没有数据的时候输出的是等待，不是编造。
- __个人数据不出本机。__ 持仓、理由、提醒全在本地存储，按 `X-User-Id` 隔离；服务对个人路径未登录直接 401。
- __MIT，可拆着用。__ 只要数据层、只要市场页、只要分析师引擎，都可以单独跑。

### 和「直接问大模型」比

|  | 直接问大模型 | 炒股软件的 AI | __Stock Agent Assistant__ |
| --- | --- | --- | --- |
| 结论基于当日真实数据 | ❌ 训练记忆 | ✅ | ✅ Tushare 实时取数 |
| 每句话可核对依据 | ❌ | ❌ | ✅ 依据按钮 |
| 多空辩论 + 裁决结构 | ❌ | ❌ | ✅ |
| 市场阶段/资金口径固定 | ❌ 每次不一样 | ✅ 但不可见 | ✅ 口径开源可审 |
| 判断会标注过期/缺数 | ❌ | 部分 | ✅ |
| 自选/持仓/理由本地私有 | — | ❌ 上云 | ✅ 本机 |
| 开源可自部署 | — | ❌ | ✅ MIT |

### 适合你如果…

- 你想要的是 __研究助理__，不是荐股机器：帮你把证据摆齐、把多空逻辑吵明白，决定自己拿。
- 你受够了 AI 聊股票时 __一本正经的过时数字__，想要每个结论都能点开看数据来源。
- 你愿意自己注册 Tushare、配一个模型 key，换回 __完全在自己机器上跑__ 的私有研判系统。

__跳过它如果__ 你想要的是「明天买哪只能涨停」——这个项目从设计上就不做预测，也请在 issue 里不要问这类问题。

## 内部是怎么工作的

### 数据层（market_tape）

全项目只有一个取数入口 [`app/services/market_tape.py`](app/services/market_tape.py)：日频序列统一「收盘后强求追到上一交易日」的口径，带内存 + 磁盘两级缓存、并发去重（single-flight）、盘后失效重取。行情、两融、涨跌停、板块资金、ETF 份额都从这里走，避免同一只票按不同窗口重复打接口。

### 市场层

- __钱有没有进来__：成交额 / 宽基 ETF 净申购 / 两融三路并看，每路用 Mz（取对数后偏离自身近 60 天常态几个标准差）分档。
- __国家队__：宽基 ETF 份额是 T+1 的，出手日、托市段、赎回迹象都从份额序列推，数没到就明说「判不了」。
- __情绪阶段__：涨跌停结构 + 追涨赚钱效应驱动冰点→修复→发酵→高潮→退潮五档阶段机，阶段变了结论跟着变。
- __板块双榜__：「在攻」（量价 + 资金同向）与「在出」（钱在撤）分桶，避免把纯涨跌当成资金行为。

### 分析师层

六位维度分析师 + 看多/看空 + 主分析师，基于 LangGraph 编排，SSE 流式输出。每条回复的结构是：先列「调用了哪些工具」，再给带结论分级的判断；主分析师裁决固定包含「采纳了谁、未采纳谁、得分、不过、如果我错了」。设计文档见 [`docs/analyst-conclusion-design.md`](docs/analyst-conclusion-design.md)。

### API 一览

```bash
# 个股
GET /api/stocks/basic?ts_code=002428.SZ          # 基础信息/行情
GET /api/v1/stocks/{code}/dimensions/market/detail
GET /api/v1/stocks/{code}/dimensions/volume_flow/stream?window=5
GET /api/v1/stocks/{code}/chief/detail            # 主分析师裁决详情

# 市场
GET /api/v1/market/summary                        # 市场页总览（钱/情绪/板块/外盘）
GET /api/v1/market/tab/team                       # 国家队（ETF 份额）
GET /api/v1/market/hot-news                       # 今日热闻
GET /api/v1/market/overseas                       # 外盘指数 + 韩股

# 个人（需 X-User-Id）
GET  /api/v1/holdings/wishes                      # 想买列表
POST /api/v1/holdings/wishes/preview              # 加入想买
GET  /api/v1/watchlist                            # 自选批量诊断
```

启动后终端会打印全部路由清单。个人数据接口需带 `X-User-Id` 请求头，多用户数据严格隔离。

### 目录结构

```
app/
  main.py              # 入口：python -m app.main
  config.py            # 超时/重试/缓存有效期，只在这里改
  api/                 # HTTP 路由（stocks / v1 / holdings / shared）
  services/            # 43 个数据服务：market_tape 取数底座、market_page 市场页、
                       #   market_team 国家队、各维度 detail/reply…
  agents/              # 分析师引擎：编排、取数器、展示层
  reviews/             # 资金面/基本面/财报/板块的 AI 体检（后台生成）
  scheduler/ notify/   # 定时任务与通知（盘前简报/收盘复盘，企微 webhook）
  auth/                # X-User-Id 用户隔离
docs/                  # 设计文档：分析师结论、调度与通知
tests/                 # 70 个测试文件，pytest 直跑
data/                  # 运行时生成：market.db + 日频缓存（可 STOCK_DATA_DIR 外移）
```

## 常见问题

__它给我买卖建议吗？__

不。所有输出都是「研究分析，不构成投资建议」：方向判断附带得分和失效条件，操作提醒是条件式的（「如果跌破 X 则原逻辑作废」），不是指令。

__数据要钱吗？__

Tushare 注册即送的积分档对本项目用到的接口基本够用（日线、资金流、两融、财报、ETF 份额）；个别高阶接口需要更多积分，缺了会明确报「没取到」而不是降级编造。

__没有桌面客户端能用吗？__

能。本仓库是纯 HTTP 服务，curl / 你自己的前端 / 任何能发请求的 agent 都能接。配套客户端是独立仓库，只依赖这套 API。

__模型必须用 DeepSeek 吗？__

不。任何 OpenAI 兼容接口都行，改 `LLM_BASE_URL` + `LLM_MODEL` 即可。模型越弱，辩论和裁决的质量下降越明显，但数据层的结论不受影响。

__为什么我的问题没有立刻回答？__

一次完整研判是九位分析师串行取数 + 发言，首次约一两分钟；SSE 流式逐条吐出，之后同一只票读缓存。

## 开发

```bash
pytest                    # 全量测试
pytest tests/test_market_tape.py -q     # 单个模块
```

测试大量使用 fixture 快照（`tests/fixtures/`），不依赖外部接口即可跑。改口径请同步改测试——口径就是这个项目的本体。

## 贡献

欢迎 issue 和 PR：数据口径的修正、新的维度分析师、通知渠道、文档。开 PR 前请跑一遍 `pytest`。

## License

MIT. See [LICENSE](LICENSE).

---

## 免责声明

本项目输出的一切内容均为基于公开数据的 __研究分析__，描述的是历史与现状的统计特征，__不构成任何投资建议__，也不预测证券价格涨跌。据此操作，风险自负。数据来自公开渠道，可能存在延迟、缺失或错误；涉及个人的功能（持仓、提醒等）仅为本地记录工具。
