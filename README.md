# Mail Statement Parser

一个基于 POP3 邮件拉取、规则解析和 SQLite 落库的信用卡账单处理工具。

当前支持银行：

- HX（华夏银行）
- CMB（招商银行）
- SPDB（浦发银行）
- CMBC（民生银行）

## 功能概览

- 按关键词批量下载最近 N 个月账单邮件
- 按银行规则解析账单字段
- 校验账单字段完整性和金额关系
- 将账单主记录、校验运行记录、交易明细写入 SQLite
- 生成最近账单、银行/月汇总、对账差异、金额阈值明细查询

## 项目结构

- [mail_client.py](mail_client.py)：命令行入口
- [statement_db.py](statement_db.py)：SQLite 持久化与查询
- [statement_models.py](statement_models.py)：数据模型
- [rules/CMB_TEMPLATE_V1.json](rules/CMB_TEMPLATE_V1.json)：招商规则模板
- [rules/CMBC_TEMPLATE_V1.json](rules/CMBC_TEMPLATE_V1.json)：民生规则模板
- [rules/HX_TEMPLATE_V1.json](rules/HX_TEMPLATE_V1.json)：华夏规则模板
- [rules/SPDB_TEMPLATE_V1.json](rules/SPDB_TEMPLATE_V1.json)：浦发规则模板
- [email-config.example.json](email-config.example.json)：示例邮箱配置

## 环境要求

- Python 3.11+
- Windows PowerShell 或任意可运行 Python 的终端

## 配置

项目中的 [email-config.example.json](email-config.example.json) 是脱敏后的示例配置。

推荐做法：

- 复制一份为 `email-config.local.json`
- 在 `email-config.local.json` 中填写你自己的邮箱参数
- `email-config.local.json` 已加入 [.gitignore](.gitignore)，不会被推送到 GitHub

程序读取优先级：

1. `email-config.local.json`
2. `email-config.json`
3. `email-config.example.json`

示例配置：

```json
{
  "email": {
    "provider": "163",
    "account": "yourmail@163.com",
    "authCode": "authCode"
  }
}
```

## 快速开始

```powershell
python .\mail_client.py test
python .\mail_client.py initdb
python .\mail_client.py download_bank_bills 3
python .\mail_client.py validate_bank_bills 3
python .\mail_client.py recent 3
python .\mail_client.py report 3
python .\mail_client.py reconcile 3 1.0
```

## 常用命令

```powershell
python .\mail_client.py test
python .\mail_client.py initdb
python .\mail_client.py search "民生信用卡 电子对账单" 50
python .\mail_client.py download <uid> --md
python .\mail_client.py download_bank_bills 3
python .\mail_client.py validate <uid>
python .\mail_client.py validate_bank_bills 3
python .\mail_client.py recent 3
python .\mail_client.py report 3
python .\mail_client.py reconcile 3 1.0
python .\mail_client.py txns_over 500
python .\mail_client.py txns_over 500 3
```

## 数据说明

- `statements`：账单主表
- `validation_runs`：每次校验执行日志
- `validation_issues`：校验错误与告警
- `statement_transactions`：账单交易明细

说明：

- `report`、`recent`、`reconcile`、`txns_over` 均基于 SQLite 数据库查询
- `validation-reports/` 目录下的 JSON 仅用于人工排查和回溯，不作为报表查询数据源

## 隐私与发布说明

仓库已通过 [.gitignore](.gitignore) 排除以下本地运行产物：

- `email-config.local.json`
- `email-downloads/`
- `validation-reports/`
- `statements.db`
- `.venv/`

因此推送到 GitHub 时不会包含本地账单内容、校验报告和数据库文件。