import argparse
import json
import smtplib
import ssl
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from html import escape
from pathlib import Path
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent
HOLDINGS_PATH = BASE_DIR / "data" / "holdings.json"
EMAIL_CONFIG_PATH = BASE_DIR / "config" / "email_config.json"
STATE_PATH = BASE_DIR / "data" / "alert_state.json"
EASTMONEY_URL = (
    "https://push2.eastmoney.com/api/qt/ulist.np/get"
    "?secids={secid}&fields=f12,f14,f2,f3,f4,f15,f16,f17,f18,f20"
)
CHECK_TIMES = ("09:30", "10:30", "11:30", "13:00", "14:00")
REPORT_TIME = "15:00"


@dataclass
class Quote:
    code: str
    name: str
    price: float
    previous_close: float
    change: float
    change_percent: float
    high: float
    low: float
    open_price: float
    amount: float
    fetched_at: str


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_holdings() -> list[dict]:
    return load_json(HOLDINGS_PATH, {"holdings": []})["holdings"]


def load_email_config() -> dict:
    if not EMAIL_CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"缺少邮件配置：{EMAIL_CONFIG_PATH}\n"
            "请复制 config/email_config.example.json 为 config/email_config.json，"
            "并填入 QQ 邮箱 SMTP 授权码。"
        )
    return load_json(EMAIL_CONFIG_PATH)


def scaled(value, decimals: int) -> float:
    if value in (None, "-", ""):
        return 0.0
    return float(value) / (10 ** decimals)


def http_get_text(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    try:
        with urlopen(request, timeout=12) as response:
            return response.read().decode("utf-8")
    except Exception:
        result = subprocess.run(
            ["curl.exe", "-sS", "-L", "-A", "Mozilla/5.0", url],
            capture_output=True,
            timeout=20,
            check=True,
        )
        return result.stdout.decode("utf-8")


def fetch_quotes(secids: list[str]) -> dict[str, Quote]:
    url = EASTMONEY_URL.format(secid=",".join(secids))
    last_error = None
    for _ in range(3):
        try:
            payload = json.loads(http_get_text(url))
            rows = (payload.get("data") or {}).get("diff") or []
            if not rows:
                raise RuntimeError(f"东方财富没有返回行情：{','.join(secids)}")

            quotes = {}
            for data in rows:
                decimals = 3
                price = scaled(data.get("f2"), decimals)
                previous = scaled(data.get("f18"), decimals)
                code = str(data.get("f12") or "")
                quotes[code] = Quote(
                    code=code,
                    name=str(data.get("f14") or ""),
                    price=price,
                    previous_close=previous,
                    change=price - previous,
                    change_percent=float(data.get("f3") or 0) / 100,
                    high=scaled(data.get("f15"), decimals),
                    low=scaled(data.get("f16"), decimals),
                    open_price=scaled(data.get("f17"), decimals),
                    amount=float(data.get("f20") or 0),
                    fetched_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
            return quotes
        except Exception as exc:
            last_error = exc
            time.sleep(1)
    raise RuntimeError(f"拉取行情失败：{','.join(secids)}，{last_error}")


def send_email(subject: str, body: str, html_body: str | None = None) -> None:
    config = load_email_config()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config["from_email"]
    msg["To"] = config.get("to_email", "1204926020@qq.com")
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(config["smtp_host"], int(config.get("smtp_port", 465)), context=context) as server:
        server.login(config["smtp_user"], config["smtp_password"])
        server.send_message(msg)


def hit_thresholds(holding: dict, price: float) -> list[dict]:
    return [item for item in holding["thresholds"] if price <= float(item["price"])]


def deepest_threshold(thresholds: list[dict]) -> dict | None:
    if not thresholds:
        return None
    return min(thresholds, key=lambda item: item["drawdown"])


def run_alert_check(send: bool = True) -> str:
    holdings = load_holdings()
    state = load_json(STATE_PATH, {"sent_alerts": {}, "jobs": {}})
    today = datetime.now().strftime("%Y-%m-%d")
    sent_alerts = state.setdefault("sent_alerts", {}).setdefault(today, {})
    quotes = fetch_quotes([holding["secid"] for holding in holdings])
    lines = []

    for holding in holdings:
        quote = quotes[holding["code"]]
        cost = float(holding["cost_price"])
        cost_drawdown = (quote.price / cost - 1) * 100
        threshold = deepest_threshold(hit_thresholds(holding, quote.price))

        lines.append(
            f"{holding['name']}({holding['code']}): 当前 {quote.price:.3f}，"
            f"成本 {cost:.4f}，较成本 {cost_drawdown:.2f}%"
        )

        if not threshold:
            continue

        last_sent = sent_alerts.get(holding["code"])
        current_level = int(threshold["drawdown"])
        if last_sent is not None and current_level >= int(last_sent):
            continue

        subject = f"ETF加仓告警：{holding['name']} 跌破 {current_level}% 档"
        body = build_alert_body(holding, quote, threshold, cost_drawdown)
        if send:
            send_email(subject, body, build_alert_html(holding, quote, threshold, cost_drawdown))
        sent_alerts[holding["code"]] = current_level
        lines.append(f"  已触发：{current_level}%，{threshold['note']}")

    save_json(STATE_PATH, state)
    return "\n".join(lines)


def build_alert_body(holding: dict, quote: Quote, threshold: dict, cost_drawdown: float) -> str:
    market_value = quote.price * int(holding["shares"])
    return (
        f"{holding['name']}（{holding['code']}）触发加仓告警\n\n"
        f"当前价格：{quote.price:.3f}\n"
        f"持仓成本：{holding['cost_price']:.4f}\n"
        f"当前持仓：{holding['shares']}份\n"
        f"当前市值：{market_value:.2f}\n"
        f"较成本跌幅：{cost_drawdown:.2f}%\n"
        f"触发档位：{threshold['drawdown']}%，对应价格 {threshold['price']:.3f}\n"
        f"加仓建议：{threshold['note']}\n\n"
        f"今日涨跌：{quote.change:+.3f}（{quote.change_percent:+.2f}%）\n"
        f"今日开盘：{quote.open_price:.3f}\n"
        f"今日最高：{quote.high:.3f}\n"
        f"今日最低：{quote.low:.3f}\n"
        f"抓取时间：{quote.fetched_at}\n"
    )


def build_alert_html(holding: dict, quote: Quote, threshold: dict, cost_drawdown: float) -> str:
    market_value = quote.price * int(holding["shares"])
    return build_html_page(
        title="ETF加仓告警",
        subtitle=f"{holding['name']} 跌破 {threshold['drawdown']}% 档",
        cards=[
            ("当前价格", f"{quote.price:.3f}"),
            ("持仓成本", f"{holding['cost_price']:.4f}"),
            ("较成本", f"{cost_drawdown:.2f}%"),
            ("当前市值", f"{market_value:.2f}"),
        ],
        body=f"""
          <div class="notice">
            <div class="notice-title">加仓建议</div>
            <div class="notice-text">{escape(str(threshold['note']))}</div>
            <div class="notice-sub">触发价格：{threshold['price']:.3f}，持仓：{holding['shares']}份</div>
          </div>
          <table>
            <tr><th>代码</th><td>{escape(holding['code'])}</td></tr>
            <tr><th>今日涨跌</th><td>{quote.change:+.3f}（{quote.change_percent:+.2f}%）</td></tr>
            <tr><th>开盘 / 最高 / 最低</th><td>{quote.open_price:.3f} / {quote.high:.3f} / {quote.low:.3f}</td></tr>
            <tr><th>抓取时间</th><td>{escape(quote.fetched_at)}</td></tr>
          </table>
        """,
    )


def build_daily_report() -> str:
    rows = collect_report_rows()
    lines = [f"ETF今日涨跌表 - {datetime.now().strftime('%Y-%m-%d')}", ""]
    for holding, quote, cost_return, market_value in rows:
        lines.append(
            f"{holding['name']}（{holding['code']}）\n"
            f"当前价：{quote.price:.3f}\n"
            f"今日涨跌：{quote.change:+.3f}（{quote.change_percent:+.2f}%）\n"
            f"较成本：{cost_return:+.2f}%\n"
            f"持仓：{holding['shares']}份\n"
            f"当前市值：{market_value:.2f}\n"
        )
    lines.append(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines)


def build_daily_report_html() -> str:
    rows = collect_report_rows()
    total_value = sum(row[3] for row in rows)
    table_rows = []
    for holding, quote, cost_return, market_value in rows:
        day_class = "up" if quote.change >= 0 else "down"
        cost_class = "up" if cost_return >= 0 else "down"
        table_rows.append(
            f"""
            <tr>
              <td>
                <div class="asset">{escape(holding['name'])}</div>
                <div class="code">{escape(holding['code'])}</div>
              </td>
              <td class="num">{quote.price:.3f}</td>
              <td class="num {day_class}">{quote.change:+.3f}<br><span>{quote.change_percent:+.2f}%</span></td>
              <td class="num {cost_class}">{cost_return:+.2f}%</td>
              <td class="num">{holding['shares']}份</td>
              <td class="num">{market_value:.2f}</td>
            </tr>
            """
        )

    return build_html_page(
        title="ETF今日涨跌表",
        subtitle=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        cards=[
            ("持仓数量", f"{len(rows)}只"),
            ("当前市值", f"{total_value:.2f}"),
            ("收件邮箱", "1204926020@qq.com"),
        ],
        body=f"""
          <table>
            <thead>
              <tr>
                <th>资产</th>
                <th>当前价</th>
                <th>今日涨跌</th>
                <th>较成本</th>
                <th>持仓</th>
                <th>市值</th>
              </tr>
            </thead>
            <tbody>{''.join(table_rows)}</tbody>
          </table>
        """,
    )


def collect_report_rows() -> list[tuple[dict, Quote, float, float]]:
    holdings = load_holdings()
    quotes = fetch_quotes([holding["secid"] for holding in holdings])
    rows = []
    for holding in holdings:
        quote = quotes[holding["code"]]
        cost = float(holding["cost_price"])
        cost_return = (quote.price / cost - 1) * 100
        market_value = quote.price * int(holding["shares"])
        rows.append((holding, quote, cost_return, market_value))
    return rows


def build_html_page(title: str, subtitle: str, cards: list[tuple[str, str]], body: str) -> str:
    card_html = "".join(
        f"""
        <div class="card">
          <div class="label">{escape(label)}</div>
          <div class="value">{escape(value)}</div>
        </div>
        """
        for label, value in cards
    )
    return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      body {{
        margin: 0;
        padding: 0;
        background: #f4f6f8;
        color: #1f2933;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, "Microsoft YaHei", sans-serif;
      }}
      .wrap {{
        max-width: 680px;
        margin: 0 auto;
        padding: 18px 12px 24px;
      }}
      .panel {{
        background: #ffffff;
        border: 1px solid #e5e7eb;
        border-radius: 10px;
        overflow: hidden;
      }}
      .header {{
        padding: 18px 16px;
        background: #111827;
        color: #ffffff;
      }}
      h1 {{
        margin: 0;
        font-size: 22px;
        line-height: 1.25;
      }}
      .subtitle {{
        margin-top: 6px;
        color: #cbd5e1;
        font-size: 13px;
      }}
      .cards {{
        display: table;
        width: 100%;
        table-layout: fixed;
        border-bottom: 1px solid #e5e7eb;
      }}
      .card {{
        display: table-cell;
        padding: 12px 10px;
        border-right: 1px solid #e5e7eb;
      }}
      .card:last-child {{
        border-right: 0;
      }}
      .label {{
        color: #6b7280;
        font-size: 12px;
      }}
      .value {{
        margin-top: 4px;
        font-size: 18px;
        font-weight: 700;
        white-space: nowrap;
      }}
      table {{
        width: 100%;
        border-collapse: collapse;
        font-size: 14px;
      }}
      th, td {{
        padding: 11px 8px;
        border-bottom: 1px solid #edf0f3;
        text-align: left;
        vertical-align: middle;
      }}
      th {{
        color: #64748b;
        background: #f8fafc;
        font-weight: 600;
        white-space: nowrap;
      }}
      .num {{
        text-align: right;
        white-space: nowrap;
      }}
      .asset {{
        font-weight: 700;
      }}
      .code {{
        margin-top: 2px;
        color: #64748b;
        font-size: 12px;
      }}
      .up {{
        color: #0f9f61;
        font-weight: 700;
      }}
      .down {{
        color: #dc2626;
        font-weight: 700;
      }}
      .notice {{
        margin: 14px;
        padding: 14px;
        border-radius: 8px;
        background: #fff7ed;
        border: 1px solid #fed7aa;
      }}
      .notice-title {{
        color: #9a3412;
        font-size: 13px;
        font-weight: 700;
      }}
      .notice-text {{
        margin-top: 6px;
        font-size: 18px;
        font-weight: 700;
      }}
      .notice-sub {{
        margin-top: 6px;
        color: #7c2d12;
        font-size: 13px;
      }}
      @media screen and (max-width: 480px) {{
        .wrap {{
          padding: 10px 6px 18px;
        }}
        h1 {{
          font-size: 20px;
        }}
        .card {{
          display: block;
          border-right: 0;
          border-bottom: 1px solid #e5e7eb;
        }}
        th, td {{
          padding: 9px 6px;
          font-size: 13px;
        }}
        .value {{
          font-size: 16px;
        }}
      }}
    </style>
  </head>
  <body>
    <div class="wrap">
      <div class="panel">
        <div class="header">
          <h1>{escape(title)}</h1>
          <div class="subtitle">{escape(subtitle)}</div>
        </div>
        <div class="cards">{card_html}</div>
        {body}
      </div>
    </div>
  </body>
</html>"""


def send_daily_report() -> str:
    body = build_daily_report()
    send_email(
        f"ETF今日涨跌表 {datetime.now().strftime('%Y-%m-%d')}",
        body,
        build_daily_report_html(),
    )
    return body


def is_workday(now: datetime) -> bool:
    return now.weekday() < 5


def daemon_loop() -> None:
    print("ETF监控已启动。按 Ctrl+C 停止。")
    while True:
        now = datetime.now()
        state = load_json(STATE_PATH, {"sent_alerts": {}, "jobs": {}})
        jobs = state.setdefault("jobs", {})
        day = now.strftime("%Y-%m-%d")
        minute = now.strftime("%H:%M")

        if is_workday(now) and minute in CHECK_TIMES:
            job_key = f"{day}:check:{minute}"
            if not jobs.get(job_key):
                print(run_alert_check(send=True))
                jobs[job_key] = True
                save_json(STATE_PATH, state)

        if is_workday(now) and minute == REPORT_TIME:
            job_key = f"{day}:report:{minute}"
            if not jobs.get(job_key):
                print(send_daily_report())
                jobs[job_key] = True
                save_json(STATE_PATH, state)

        time.sleep(20)


def main() -> None:
    parser = argparse.ArgumentParser(description="ETF监控加仓告警")
    parser.add_argument("--daemon", action="store_true", help="常驻运行，按交易时间自动监控")
    parser.add_argument("--check", action="store_true", help="立即执行一次档位告警检查")
    parser.add_argument("--report", action="store_true", help="立即发送一次今日涨跌表")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不发送邮件")
    args = parser.parse_args()

    if args.daemon:
        daemon_loop()
    elif args.report:
        body = build_daily_report() if args.dry_run else send_daily_report()
        print(body)
    else:
        print(run_alert_check(send=not args.dry_run))


if __name__ == "__main__":
    main()
