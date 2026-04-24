# 邮件账单系统操作说明（够用版）

## 1. 目标

这份说明用于日常操作，核心流程是：

1. 批量下载最近 3 个月银行账单邮件
2. 对指定邮件做规则解析与校验（同时写入 SQLite）
3. 查询最近账单、汇总报表、对账差异

系统当前支持银行：HX / CMB / SPDB / CMBC。

---

## 2. 环境与文件

在项目根目录执行命令：

- `mail_client.py`：主入口
- `rules/*.json`：银行规则模板
- `statements.db`：SQLite 数据库
- `email-downloads/`：下载邮件落地目录
- `validation-reports/`：校验报告输出目录

---

## 3. 首次初始化

### 3.1 测试邮箱连接

```powershell
python .\mail_client.py test
```

预期输出包含：`SMTP OK`、`POP3 OK`（IMAP 可选）。

### 3.2 初始化数据库

```powershell
python .\mail_client.py initdb
```

会创建/初始化 SQLite 表结构。

---

## 4. 日常操作流程（推荐）

### 步骤 1：批量下载最近 3 个月账单

```powershell
python .\mail_client.py download_bank_bills 3
```

说明：

1. 这是“专用指令”，会按规则中的账单主题关键字筛选
2. 只下载账单类邮件（非账单通知会被过滤）
3. 默认写入 `email-downloads/`

可用别名：

```powershell
python .\mail_client.py exec3m 3
```

### 步骤 2：对邮件做解析校验并写库

按 UID 执行：

```powershell
python .\mail_client.py validate 10560
```

说明：

1. `validate` 会解析字段、执行规则校验、写 JSON 报告
2. 同时写库：账单主记录、校验运行记录、交易明细（如可解析）

无需 UID 的批量写库（推荐）：

```powershell
python .\mail_client.py validate_bank_bills 3
```

说明：

1. 自动筛选最近 N 个月账单邮件
2. 自动逐封执行 validate 并写入 SQLite
3. 不需要手工输入 UID

### 步骤 3：查询数据库结果

最近账单（默认 3 个月）：

```powershell
python .\mail_client.py recent 3
```

银行/月汇总：

```powershell
python .\mail_client.py report 3
```

对账差异（容差 1.0）：

```powershell
python .\mail_client.py reconcile 3 1.0
```

---

## 5. 常用补充命令

按关键字搜索邮件：

```powershell
python .\mail_client.py search "民生信用卡 电子对账单" 50
```

下载单封邮件（UID）：

```powershell
python .\mail_client.py download 10560 --md
```

查看规则匹配：

```powershell
python .\mail_client.py classify 10560
```

---

## 6. 写库与幂等说明

### 6.1 哪些命令会写库

会写库：

- `validate <uid>`
- `validate_bank_bills [months]`

不会写库：

- `download_bank_bills`
- `download`
- `search`
- `read`
- `classify`

### 6.2 幂等性（当前行为）

1. 账单主表写入：幂等（同 uid+bank_code 会 upsert）
2. 交易明细写入：幂等（同 uid+bank_code 先删后插）
3. 校验运行日志：非幂等（每次 validate 都会新增一次 run 记录）
4. 下载文件：非幂等（文件名含时间戳，重复执行会产生新文件）

---

## 7. 建议操作习惯

1. 每次规则调整后，固定回归 2-4 个历史 UID
2. 下载与写库分开执行：先 `download_bank_bills`，后 `validate`
3. 以 `report` + `reconcile` 作为每次调整后的验收出口

---

## 8. 常见问题

### Q1：为什么下载成功但库里没有新数据？

因为下载命令只落地文件，不自动写库。需要执行 `validate <uid>`。

### Q2：同一封邮件重复下载为什么出现多个文件？

当前下载文件名带时间戳，设计上保留历史版本。

### Q3：为什么 `search` 偶尔报错？

POP3 历史邮件可能出现异常编码/超长行，重试并缩小关键字范围通常可绕过。

---

## 9. 一键最小执行清单

```powershell
python .\mail_client.py test
python .\mail_client.py initdb
python .\mail_client.py download_bank_bills 3
python .\mail_client.py validate_bank_bills 3
python .\mail_client.py recent 3
python .\mail_client.py report 3
python .\mail_client.py reconcile 3 1.0
```
