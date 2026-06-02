# 学在城院 (TronClass) 作业附件下载器

批量下载 courses.hzcu.edu.cn 课程下**所有作业的关联附件**与**我的提交**，按作业分文件夹保存，并生成清单。支持命令行与图形界面两种用法。

## 功能

- 统一身份认证 (CAS) 登录，密码按官网同款 AES-ECB/Pkcs7 加密提交
- 抓取课程全部作业，解析每个作业的题目附件
- 可选下载自己已提交的作业文件
- 按 `作业序号_标题/{题目附件, 我的提交}` 分目录保存
- 生成 `manifest.json` 清单（含每个附件的下载链接与本地路径）
- Tkinter 图形界面：填表、实时日志、进度条

## 安装

```bash
pip install -r requirements.txt
```

依赖：`requests`、`pycryptodome`、`python-dotenv`（GUI 用标准库 `tkinter`，无需额外安装）。

## 配置

复制 `.env.example` 为 `.env` 并填写：

```ini
HZCU_USERNAME=你的学号
HZCU_PASSWORD=你的密码
COURSE_ID=53472        # 课程作业页 URL 里的数字：/course/<COURSE_ID>/homework
OUTPUT_DIR=downloads
```

## 使用

命令行：

```bash
python main.py                       # 用 .env 的配置
python main.py --course 53472        # 指定课程
python main.py --no-submissions      # 只下题目附件，不下我的提交
python main.py --list-only           # 仅列出附件与链接，不下载
```

图形界面：

```bash
python gui.py
```

填入账号、密码、课程 ID，选择保存目录，点击「开始下载」。可勾选是否下载我的提交、是否仅列出。

## 输出结构

```
downloads/course_53472/
├── 01_第一次作业/
│   ├── 题目附件/        # 老师发布的附件
│   │   └── 实验要求.pdf
│   └── 我的提交/        # 自己提交的文件（若有）
│       └── 实验报告.docx
├── 02_.../
└── manifest.json        # 全部作业与附件清单
```

## 文件说明

| 文件 | 作用 |
|------|------|
| `auth.py` | CAS 登录、密码 AES 加密 |
| `crawler.py` | 调 TronClass API 取作业列表、作业详情、我的提交 |
| `attachments.py` | 从详情 JSON 递归提取附件并构造下载链接 |
| `downloader.py` | 流式下载、文件名清洗与去重、写清单 |
| `core.py` | 串联登录→抓取→下载的核心流程（CLI/GUI 共用） |
| `main.py` | 命令行入口 |
| `gui.py` | 图形界面入口 |

## 说明

- 加密密钥与加密方式取自登录页脚本，已与官网 CryptoJS 输出逐字节比对一致。
- 不同 TronClass 版本接口字段略有差异，代码对作业列表/详情/提交端点做了多路兼容回退。
- 若账号开启了短信验证码或图形验证码，纯 HTTP 登录可能失败，此时需改用浏览器登录方式。
- 账号密码仅保存在本地 `.env`，已加入 `.gitignore`，不会上传。
