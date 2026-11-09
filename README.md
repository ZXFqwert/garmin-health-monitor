# li33-life

给自己的 personal agent（我用的是 [Hermes](https://github.com/NousResearch/hermes-agent)）接上**个人生活数据**的小工具：佳明手表 + 一个 IMAP 邮箱。单文件、只读、命令行输出就是给模型看的中文摘要。

> 这个仓库原来是 `garmin-health-monitor`（2025 年用 garth 写的健康告警脚本，登录方式已失效），2026-11 起换成现在的实现。

## 能做什么

```text
life.py garmin-login            本人在终端跑一次：输入佳明账号密码（不回显），只保存登录令牌，不存密码
life.py health [--days 1]       睡眠、HRV、静息心率、身体电量、压力、训练准备度、步数、运动
life.py mail [--days 3]         最近收件箱列表（不会标成已读）；--unread 只看未读；--json 给程序用
life.py mail-read m:UID         读一封（纯文本，附件只列名字）
life.py mail-search 关键词       按发件人、主题找
life.py mail-unlock             登录失败被锁住后，换好授权码再解锁
life.py status                  两个接口的连接状态
```

输出示例：

```text
佳明手表（11-08 起）：
11-08 当晚睡眠 6小时03分（00:29–06:32），睡眠分 85｜深睡 0小时57分、REM 1小时27分
      静息心率 48｜夜间 HRV 99（周均 92，BALANCED）｜身体电量 最高 84 / 最低 11 / 日终 17
      步数 11954｜强度分钟 10｜活动消耗 372 千卡
```

## 安装

```bash
git clone https://github.com/ZXFqwert/garmin-health-monitor.git ~/.local/share/li33-life
cd ~/.local/share/li33-life
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
mkdir -p ~/.config/li33-life && cp config.example.json ~/.config/li33-life/config.json   # 改成自己的
./venv/bin/python life.py garmin-login
security add-generic-password -a 你的邮箱 -s li33-life-mail -U -w    # 粘贴邮箱的“客户端授权码”
./venv/bin/python life.py status
```

升级：`git pull && ./venv/bin/pip install -r requirements.txt`。

## 配置 `~/.config/li33-life/config.json`

| 键 | 含义 |
|---|---|
| `garmin_cn` | `true` 用佳明中国（connect.garmin.cn），`false` 用国际版 |
| `mail_user` / `mail_host` / `mail_port` | IMAP 账号与服务器（只支持 993 IMAPS） |
| `mail_label` | 输出里怎么称呼这个邮箱 |
| `mail_keychain_service` | 授权码在 macOS 钥匙串里的服务名 |
| `mail_pins` | 可选。服务器证书 SHA-256 指纹列表；有些学校邮箱不发中间证书、验不了链，就钉指纹（对不上就拒绝连接） |
| `mail_min_gap_seconds` | 两次真正连邮箱的最小间隔，其余时间用本地缓存 |
| `utc_offset_hours` | 本地时区 |

查证书指纹：

```bash
openssl s_client -connect mail.example.edu.cn:993 </dev/null 2>/dev/null | openssl x509 -outform DER | shasum -a 256
```

## 设计取舍

- **只读**：邮箱用 `EXAMINE` + `BODY.PEEK`，不改已读状态，不能发信、删信；佳明只调读接口。
- **不存密码**：佳明只存 `garminconnect` 的令牌（`~/.config/li33-life/garmin-tokens/`，权限 600）；邮箱授权码放钥匙串。
- **失败即上锁**：学校邮箱对反复失败的登录会要求改密码，所以登录失败一次就写锁文件，不再重试，直到手动 `mail-unlock`。
- **限流 + 缓存**：数据缓存在本地 `life.db`（SQLite），定时任务频繁调用也不会频繁连服务器。
- **日期对齐**：佳明把睡眠记在醒来那天；这里把“某天白天的活动”和“当晚的睡眠/HRV”放在同一行，读起来更符合直觉。

## 接到 agent 上

- `examples/hermes-skill-life-data.md`：Hermes 技能（放到 `~/.hermes/skills/productivity/life-data/SKILL.md`），告诉模型什么时候、怎么调这些命令，以及解读分寸。
- `examples/morning_snippet.py`：早上简报的定时脚本里加的一段，把昨晚身体状态和最近一天的邮件拼进素材。

## License

MIT
