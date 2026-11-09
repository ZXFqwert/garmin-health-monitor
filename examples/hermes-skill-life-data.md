---
name: life-data
description: 机主的佳明手表数据和邮箱（li33-life，只读）.
version: 1.0.0
author: you
platforms: [macos]
created_by: user
---

# 机主的生活数据（只读）

命令都用 terminal 跑，前缀记作 `LIFE`：

```bash
LIFE="$HOME/.local/share/li33-life/venv/bin/python $HOME/.local/share/li33-life/life.py"
```

## 佳明手表
- `$LIFE health`：昨晚睡眠（时长、起止、睡眠分、深睡/REM/清醒）、静息心率、夜间 HRV、身体电量、平均压力、训练准备度、步数、强度分钟、运动。
- `$LIFE health --days 7`：最近一周，看趋势；`--today` 把今天（数据不全）也带上；`--refresh` 强制重新取。
- 解读时和他自己的周均比，不和"标准值"比；睡得差、HRV 掉、身体电量低就一句话说清可能原因和今天怎么安排，别吓人、别说教。

## 邮箱
- `$LIFE mail --days 3`：最近 3 天收件箱（`--unread` 只看未读）。列表里的 `m:编号` 用来读全文。
- `$LIFE mail-read m:123`：读一封（纯文本，含附件名）。
- `$LIFE mail-search 关键词 --days 30`：按发件人、主题找。
- 全程只读：不会把邮件标成已读，不能发信、删信。机主要回信就起草好发给他，让他自己发。
- 判断要紧程度：导师、学院、研究生院、教务、保密、截止日期、需要他回复或提交的，优先说；通知类、广告类一句带过或不提。

## 出错时
- 提示"还没登录 / 授权码还没存 / 已上锁"：照原话告诉机主要他做什么，不要自己重试。
- 邮箱登录失败会自动上锁、不再重试（以前反复失败登录导致学校邮箱要求改密码）。
