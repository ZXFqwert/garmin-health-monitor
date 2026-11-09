#!/usr/bin/env python3
"""li33-life：个人生活数据接口（佳明手表 + 一个 IMAP 邮箱），给自建 agent（如 Hermes）和每日简报用。只读。

  life.py garmin-login            本人在终端跑一次：输入佳明账号密码（不回显），只保存登录令牌，不存密码
  life.py health [--days N]       最近 N 天：每天白天的步数、压力、身体电量、运动 + 当晚睡眠和 HRV（默认 1 = 昨天和昨晚）
  life.py mail [--days N] [--unread] [--limit N] [--json]   收件箱列表（不会把邮件标成已读）
  life.py mail-read UID           读一封（纯文本，截断）
  life.py mail-search 关键词 [--days N]
  life.py mail-unlock             登录失败被锁住后，换好授权码再解锁
  life.py status                  两个接口的连接状态

配置在 ~/.config/li33-life/config.json（见 config.example.json）。邮箱密码用邮箱的"客户端授权码"，本人存进 macOS 钥匙串，
本脚本只在登录时读取。登录失败一次就上锁、不再重试（学校邮箱对反复失败的登录会要求改密码），直到跑 mail-unlock。
"""
from __future__ import annotations

import argparse
import datetime as dt
import email
import email.header
import email.utils
import getpass
import hashlib
import html
import imaplib
import json
import os
import re
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
CONF = HOME / ".config" / "li33-life"
DATA = HOME / ".local" / "share" / "li33-life"
TOKENS = CONF / "garmin-tokens"
MAIL_LOCK = CONF / "mail-auth-failed"
DB = DATA / "life.db"




def _config():
    try:
        return json.loads((CONF / "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


CFG = _config()
GARMIN_CN = bool(CFG.get("garmin_cn", True))          # 佳明中国账号（connect.garmin.cn）
MAIL_USER = CFG.get("mail_user", "")
MAIL_HOST = CFG.get("mail_host", "")
MAIL_PORT = int(CFG.get("mail_port", 993))
MAIL_KEYCHAIN = CFG.get("mail_keychain_service", "li33-life-mail")
MAIL_LABEL = CFG.get("mail_label", "邮箱")
# 有的学校邮箱服务器不发中间证书，Python 验不了链。配置了 mail_pins（证书 SHA-256 指纹）就改为核对指纹：
# 对不上就拒绝连接，绝不退回明文。没配就走正常的证书链验证。
MAIL_PINS = {p.replace(":", "").lower() for p in CFG.get("mail_pins", [])}
MAIL_MIN_GAP = int(CFG.get("mail_min_gap_seconds", 600))   # 两次连邮箱至少隔多久，其余时间用缓存

TZ = dt.timezone(dt.timedelta(hours=int(CFG.get("utc_offset_hours", 8))))


def now():
    return dt.datetime.now(TZ)


def db():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript("""
    create table if not exists garmin_daily(day text primary key, data text, fetched text);
    create table if not exists mail(uid integer primary key, ts text, sender text, subject text, seen integer, fetched text);
    create table if not exists meta(key text primary key, value text);
    """)
    return con


def meta(con, key, value=None):
    if value is None:
        row = con.execute("select value from meta where key=?", (key,)).fetchone()
        return row["value"] if row else None
    con.execute("insert or replace into meta(key, value) values(?,?)", (key, str(value)))
    con.commit()


# ------------------------------------------------------------------ 佳明

def garmin_client():
    from garminconnect import Garmin
    if not TOKENS.exists():
        raise SystemExit("佳明还没登录：请本人在终端运行  ~/.local/share/li33-life/venv/bin/python ~/.local/share/li33-life/life.py garmin-login")
    api = Garmin(is_cn=GARMIN_CN)
    api.login(str(TOKENS))
    return api


def garmin_login():
    from garminconnect import Garmin
    print("佳明%s账号登录（只保存登录令牌到 %s，不保存密码）" % ("中国" if GARMIN_CN else "国际", TOKENS))
    account = input("佳明账号（邮箱或手机号）：").strip()
    password = getpass.getpass("密码（输入时不显示）：")
    api = Garmin(account, password, is_cn=GARMIN_CN, prompt_mfa=lambda: input("验证码："))
    CONF.mkdir(parents=True, exist_ok=True)
    os.chmod(CONF, 0o700)
    api.login(str(TOKENS))
    dump = getattr(api.client, "dump", None)
    if callable(dump):
        dump(str(TOKENS))
    for p in [TOKENS, *TOKENS.glob("*")] if TOKENS.is_dir() else [TOKENS]:
        os.chmod(p, 0o700 if p.is_dir() else 0o600)
    name = ""
    try:
        name = api.get_full_name() or ""
    except Exception:
        pass
    print("登录成功%s。之后 Hermes 用令牌取数据，不再需要密码。" % ("：" + name if name else ""))


def _g(d, *path, default=None):
    for key in path:
        if not isinstance(d, dict):
            return default
        d = d.get(key)
    return default if d is None else d


def _hm(seconds):
    if not seconds:
        return ""
    m = int(seconds) // 60
    return "%d小时%02d分" % (m // 60, m % 60)


def garmin_day(api, con, day: dt.date, refresh=False):
    """一天的原始数据（缓存：今天以前的数据取过就不再取；今天的 30 分钟内复用）。"""
    key = day.isoformat()
    row = con.execute("select data, fetched from garmin_daily where day=?", (key,)).fetchone()
    if row and not refresh:
        age = (now() - dt.datetime.fromisoformat(row["fetched"])).total_seconds()
        if day < now().date() - dt.timedelta(days=1) or age < 1800:
            return json.loads(row["data"])
    data = {}
    for name, fn in (("summary", lambda: api.get_user_summary(key)), ("sleep", lambda: api.get_sleep_data(key)),
                     ("hrv", lambda: api.get_hrv_data(key)), ("readiness", lambda: api.get_training_readiness(key)),
                     ("activities", lambda: api.get_activities_by_date(key, key))):
        try:
            value = fn()
            data[name] = value if isinstance(value, (dict, list)) or value is None else json.loads(json.dumps(value, default=str))
        except Exception as exc:
            data[name] = {"_error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}
    con.execute("insert or replace into garmin_daily(day, data, fetched) values(?,?,?)",
                (key, json.dumps(data, ensure_ascii=False, default=str), now().isoformat()))
    con.commit()
    return data


def health_lines(data, night, day):
    """day 这一天：白天的数据（步数、压力、运动）取 day 的；睡眠、夜间 HRV、准备度取第二天早上记的那份，
    也就是 day 当晚（佳明把一夜的睡眠记在醒来那天）。"""
    night = night or {}
    s, sl, hrv = data.get("summary") or {}, _g(night, "sleep", "dailySleepDTO") or {}, _g(night, "hrv", "hrvSummary") or {}
    out = []
    if sl.get("sleepTimeSeconds"):
        score = _g(sl, "sleepScores", "overall", "value")
        start = sl.get("sleepStartTimestampLocal")
        end = sl.get("sleepEndTimestampLocal")
        span = ""
        if start and end:
            f = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%H:%M")
            span = "（%s–%s）" % (f(start), f(end))
        out.append("当晚睡眠 %s%s%s｜深睡 %s、REM %s、清醒 %s" % (
            _hm(sl["sleepTimeSeconds"]), span, "，睡眠分 %s" % score if score else "",
            _hm(sl.get("deepSleepSeconds")) or "—", _hm(sl.get("remSleepSeconds")) or "—", _hm(sl.get("awakeSleepSeconds")) or "—"))
    vitals = []
    if s.get("restingHeartRate"):
        vitals.append("静息心率 %s" % s["restingHeartRate"])
    if hrv.get("lastNightAvg"):
        vitals.append("夜间 HRV %s（周均 %s，%s）" % (hrv["lastNightAvg"], hrv.get("weeklyAvg") or "—", hrv.get("status") or "—"))
    if s.get("bodyBatteryHighestValue") is not None:
        vitals.append("身体电量 最高 %s / 最低 %s / 日终 %s" % (s.get("bodyBatteryHighestValue"), s.get("bodyBatteryLowestValue"),
                                                    s.get("bodyBatteryMostRecentValue")))
    if s.get("averageStressLevel") not in (None, -1, -2):
        vitals.append("平均压力 %s" % s["averageStressLevel"])
    readiness = night.get("readiness")
    if isinstance(readiness, list) and readiness and isinstance(readiness[0], dict) and readiness[0].get("score") is not None:
        vitals.append("训练准备度 %s（%s）" % (readiness[0]["score"], readiness[0].get("level") or ""))
    if vitals:
        out.append("｜".join(vitals))
    move = []
    if s.get("totalSteps"):
        move.append("步数 %s" % s["totalSteps"])
    mins = (s.get("moderateIntensityMinutes") or 0) + 2 * (s.get("vigorousIntensityMinutes") or 0)
    if mins:
        move.append("强度分钟 %s" % mins)
    if s.get("activeKilocalories"):
        move.append("活动消耗 %s 千卡" % int(s["activeKilocalories"]))
    acts = data.get("activities") if isinstance(data.get("activities"), list) else []
    for a in acts[:3]:
        move.append("%s %s%s" % (a.get("activityName") or _g(a, "activityType", "typeKey") or "运动", _hm(a.get("duration")),
                                 "，%.1f 公里" % (a["distance"] / 1000) if a.get("distance") else ""))
    if move:
        out.append("｜".join(move))
    if not out:
        errors = [v["_error"] for v in data.values() if isinstance(v, dict) and v.get("_error")]
        out.append("没有数据" + ("（%s）" % errors[0] if errors else "，可能手表还没同步"))
    return ["%s %s" % (day.strftime("%m-%d"), line) if i == 0 else "      " + line for i, line in enumerate(out)]


def cmd_health(args):
    api, con = garmin_client(), db()
    today = now().date()
    lines = []
    for i in range(args.days, -1 if args.today else 0, -1):
        day = today - dt.timedelta(days=i)
        night = garmin_day(api, con, day + dt.timedelta(days=1), refresh=args.refresh) if day < today else {}
        lines.extend(health_lines(garmin_day(api, con, day, refresh=args.refresh), night, day))
    print("佳明手表（%s 起）：\n%s" % ((today - dt.timedelta(days=args.days)).strftime("%m-%d"), "\n".join(lines)))


# ------------------------------------------------------------------ 邮箱

def _decode(value):
    if not value:
        return ""
    parts = []
    for text, charset in email.header.decode_header(value):
        if isinstance(text, bytes):
            for cs in (charset, "utf-8", "gb18030"):
                try:
                    text = text.decode(cs or "utf-8")
                    break
                except (LookupError, UnicodeDecodeError):
                    continue
            else:
                text = text.decode("utf-8", "replace")
        parts.append(text)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _password():
    try:
        return subprocess.run(["security", "find-generic-password", "-a", MAIL_USER, "-s", MAIL_KEYCHAIN, "-w"],
                              capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    except subprocess.CalledProcessError:
        raise SystemExit("邮箱授权码还没存：请在终端运行  security add-generic-password -a %s -s %s -U -w  然后粘贴授权码"
                         % (MAIL_USER, MAIL_KEYCHAIN))


def mail_connect():
    if not MAIL_USER or not MAIL_HOST:
        raise SystemExit("邮箱还没配置：在 %s 写上 mail_user、mail_host（参考 config.example.json）" % (CONF / "config.json"))
    if MAIL_LOCK.exists():
        raise SystemExit("邮箱上次登录失败，已上锁不再重试（%s）。请在网页邮箱确认授权码后，重新存进钥匙串，再运行 life.py mail-unlock"
                         % MAIL_LOCK.read_text(encoding="utf-8").strip())
    password = _password()
    ctx = ssl.create_default_context()
    if not MAIL_PINS:
        imap = imaplib.IMAP4_SSL(MAIL_HOST, MAIL_PORT, ssl_context=ctx, timeout=30)
        return _mail_login(imap, password)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE     # 下面手动核对指纹：比不验证安全，适合不发中间证书的服务器
    sock = socket.create_connection((MAIL_HOST, MAIL_PORT), timeout=20)
    tls = ctx.wrap_socket(sock, server_hostname=MAIL_HOST)
    pin = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
    tls.close()
    if pin not in MAIL_PINS:
        raise SystemExit("邮箱服务器证书变了（指纹 %s），为安全起见拒绝连接。确认后把新指纹加进 config.json 的 mail_pins。" % pin)

    class PinnedIMAP(imaplib.IMAP4_SSL):
        def _create_socket(self, timeout):
            s = socket.create_connection((self.host, self.port), timeout)
            t = ctx.wrap_socket(s, server_hostname=self.host)
            if hashlib.sha256(t.getpeercert(binary_form=True)).hexdigest() not in MAIL_PINS:
                t.close()
                raise SystemExit("邮箱服务器证书和钉住的不一致，拒绝连接。")
            return t

    return _mail_login(PinnedIMAP(MAIL_HOST, MAIL_PORT, timeout=30), password)


def _mail_login(imap, password):
    try:
        imap.login(MAIL_USER, password)
    except imaplib.IMAP4.error as exc:
        CONF.mkdir(parents=True, exist_ok=True)
        MAIL_LOCK.write_text("%s %s" % (now().strftime("%Y-%m-%d %H:%M"), str(exc)[:120]), encoding="utf-8")
        raise SystemExit("邮箱登录失败：%s。已上锁，不会自动重试，免得被要求改密码。" % exc)
    return imap


def mail_sync(con, days=7, force=False):
    """拉最近 N 天收件箱的信头进缓存。自动调用时 10 分钟内只连一次。"""
    last = meta(con, "mail_synced")
    if not force and last and (now() - dt.datetime.fromisoformat(last)).total_seconds() < MAIL_MIN_GAP:
        return False
    imap = mail_connect()
    try:
        imap.select("INBOX", readonly=True)
        since = (now() - dt.timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = imap.uid("search", None, "SINCE", since)
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        for chunk in [uids[i:i + 50] for i in range(0, len(uids), 50)]:
            typ, rows = imap.uid("fetch", b",".join(chunk), "(FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            for item in rows or []:
                if not isinstance(item, tuple):
                    continue
                head, body = item[0].decode("utf-8", "replace"), item[1]
                uid = int(re.search(r"UID (\d+)", head).group(1))
                msg = email.message_from_bytes(body)
                when = email.utils.parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
                ts = (when.astimezone(TZ) if when and when.tzinfo else now()).isoformat()
                name, addr = email.utils.parseaddr(_decode(msg.get("From")))
                con.execute("insert or replace into mail(uid, ts, sender, subject, seen, fetched) values(?,?,?,?,?,?)",
                            (uid, ts, ("%s <%s>" % (name, addr)) if name else addr, _decode(msg.get("Subject")),
                             1 if "\\Seen" in head else 0, now().isoformat()))
        con.commit()
        meta(con, "mail_synced", now().isoformat())
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return True


def cmd_mail(args):
    con = db()
    mail_sync(con, max(args.days, 7), force=args.refresh)
    since = (now() - dt.timedelta(days=args.days)).isoformat()
    rows = con.execute("select * from mail where ts>=? %s order by ts desc limit ?" % ("and seen=0" if args.unread else ""),
                       (since, args.limit)).fetchall()
    if args.json:
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False))
        return
    if not rows:
        print("%s：最近 %d 天没有%s邮件。" % (MAIL_LABEL, args.days, "未读" if args.unread else ""))
        return
    print("%s（最近 %d 天%s，%d 封，不会标成已读）：" % (MAIL_LABEL, args.days, "未读" if args.unread else "", len(rows)))
    for r in rows:
        print("- [m:%d] %s %s｜%s｜%s" % (r["uid"], r["ts"][5:16].replace("T", " "), "" if r["seen"] else "未读",
                                       r["sender"][:40], r["subject"][:80]))


def _text_of(msg):
    plain, htm = [], []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() != "text" or part.get_filename():
            continue
        payload = part.get_payload(decode=True) or b""
        for cs in (part.get_content_charset(), "utf-8", "gb18030"):
            try:
                text = payload.decode(cs or "utf-8")
                break
            except (LookupError, UnicodeDecodeError):
                continue
        else:
            text = payload.decode("utf-8", "replace")
        (plain if part.get_content_subtype() == "plain" else htm).append(text)
    text = "\n".join(plain) or re.sub(r"<[^>]+>", " ", html.unescape(re.sub(r"(?is)<(script|style).*?</\1>", "", "\n".join(htm))))
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t　]+", " ", text)).strip()


def cmd_mail_read(args):
    uid = str(args.uid).replace("m:", "")
    imap = mail_connect()
    try:
        imap.select("INBOX", readonly=True)
        typ, rows = imap.uid("fetch", uid, "(BODY.PEEK[])")
        raw = next((item[1] for item in rows or [] if isinstance(item, tuple)), None)
        if not raw:
            raise SystemExit("没有这封邮件：m:%s" % uid)
        msg = email.message_from_bytes(raw)
        files = [_decode(p.get_filename()) for p in msg.walk() if p.get_filename()]
        print("发件人：%s\n时间：%s\n主题：%s%s\n\n%s" % (
            _decode(msg.get("From")), msg.get("Date"), _decode(msg.get("Subject")),
            "\n附件：" + "、".join(files) if files else "", _text_of(msg)[:args.chars]))
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def cmd_mail_search(args):
    con = db()
    mail_sync(con, max(args.days, 7))
    words = [w.lower() for w in args.query.split() if w]
    since = (now() - dt.timedelta(days=args.days)).isoformat()
    rows = [r for r in con.execute("select * from mail where ts>=? order by ts desc", (since,)).fetchall()
            if all(w in (r["sender"] + " " + r["subject"]).lower() for w in words)]
    print("\n".join("- [m:%d] %s｜%s｜%s" % (r["uid"], r["ts"][5:16].replace("T", " "), r["sender"][:40], r["subject"][:80])
                    for r in rows[:20]) or "最近 %d 天的信头里没找到「%s」（只搜发件人和主题）。" % (args.days, args.query))


def cmd_status(_args):
    con = db()
    print("佳明：%s" % ("已登录（令牌在 %s）" % TOKENS if TOKENS.exists() else "未登录，需要本人跑 garmin-login"))
    last = con.execute("select max(fetched) t from garmin_daily").fetchone()["t"]
    if last:
        print("      上次取数据 %s" % last[:16].replace("T", " "))
    key = subprocess.run(["security", "find-generic-password", "-a", MAIL_USER, "-s", MAIL_KEYCHAIN],
                         capture_output=True, text=True).returncode == 0
    print("邮箱：%s%s" % ("授权码已存" if key else "授权码未存", "；上次登录失败已上锁：" + MAIL_LOCK.read_text(encoding="utf-8").strip()
                         if MAIL_LOCK.exists() else ""))
    synced = meta(con, "mail_synced")
    if synced:
        print("      上次收信 %s" % synced[:16].replace("T", " "))


def main():
    ap = argparse.ArgumentParser(description="li33-life：佳明手表 + IMAP 邮箱（只读）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("garmin-login")
    h = sub.add_parser("health")
    h.add_argument("--days", type=int, default=1)
    h.add_argument("--today", action="store_true", help="也包含今天（数据不全）")
    h.add_argument("--refresh", action="store_true")
    m = sub.add_parser("mail")
    m.add_argument("--days", type=int, default=3)
    m.add_argument("--unread", action="store_true")
    m.add_argument("--limit", type=int, default=20)
    m.add_argument("--json", action="store_true")
    m.add_argument("--refresh", action="store_true")
    r = sub.add_parser("mail-read")
    r.add_argument("uid")
    r.add_argument("--chars", type=int, default=4000)
    s = sub.add_parser("mail-search")
    s.add_argument("query")
    s.add_argument("--days", type=int, default=30)
    sub.add_parser("mail-unlock")
    sub.add_parser("status")
    args = ap.parse_args()
    if args.cmd == "garmin-login":
        garmin_login()
    elif args.cmd == "health":
        cmd_health(args)
    elif args.cmd == "mail":
        cmd_mail(args)
    elif args.cmd == "mail-read":
        cmd_mail_read(args)
    elif args.cmd == "mail-search":
        cmd_mail_search(args)
    elif args.cmd == "mail-unlock":
        MAIL_LOCK.unlink(missing_ok=True)
        print("已解锁，下次会重新登录邮箱。")
    elif args.cmd == "status":
        cmd_status(args)


if __name__ == "__main__":
    main()
