from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from tradingagents.default_config import DEFAULT_CONFIG


DEFAULT_ANALYSTS = [
    "market",
    "social",
    "news",
    "fundamentals",
    "macro",
    "smart_money",
    "volume_price",
]


def _load_dotenv_if_exists(path: str = ".env") -> None:
    """Minimal .env loader to avoid extra runtime dependency."""
    if not os.path.exists(path):
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key or key in os.environ:
                    continue
                value = value.strip().strip('"').strip("'")
                os.environ[key] = value
    except Exception:
        # Keep CLI resilient: if .env parse fails, continue with system env only.
        return


def _normalize_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if not s:
        raise ValueError("symbol 不能为空")
    m = re.search(r"(\d{6})", s)
    if not m:
        return s
    code = m.group(1)
    if s.endswith(".SH") or s.endswith(".SZ") or s.endswith(".BJ"):
        return s
    suffix = ".SH" if code.startswith(("5", "6", "9")) else ".SZ"
    return f"{code}{suffix}"


def _preprocess_argv(argv: List[str]) -> List[str]:
    """
    Allow shorthand: `tradingagents analyst --600519`
    Convert it to:  `tradingagents analyst --symbol 600519`
    """
    converted: List[str] = []
    inserted_symbol = False
    for token in argv:
        short_symbol = re.fullmatch(r"--(\d{6}(?:\.(?:SH|SZ|BJ))?)", token, flags=re.IGNORECASE)
        if short_symbol and not inserted_symbol:
            converted.extend(["--symbol", short_symbol.group(1)])
            inserted_symbol = True
            continue
        converted.append(token)
    return converted


def _build_runtime_config() -> Dict[str, Any]:
    cfg = DEFAULT_CONFIG.copy()
    cfg["llm_provider"] = os.getenv("TA_LLM_PROVIDER", cfg.get("llm_provider", "openai"))
    cfg["deep_think_llm"] = os.getenv("TA_LLM_DEEP", cfg.get("deep_think_llm", "gpt-4o"))
    cfg["quick_think_llm"] = os.getenv("TA_LLM_QUICK", cfg.get("quick_think_llm", "gpt-4o-mini"))
    cfg["backend_url"] = os.getenv("TA_BASE_URL", cfg.get("backend_url", "https://api.openai.com/v1"))
    cfg["api_key"] = os.getenv("TA_API_KEY", cfg.get("api_key", ""))
    if not cfg["api_key"]:
        raise RuntimeError("缺少 TA_API_KEY，请在 .env 或系统环境变量中配置。")
    return cfg


def _build_report_text(symbol: str, trade_date: str, final_state: Dict[str, Any]) -> str:
    decision = str(final_state.get("final_trade_decision") or "").strip()
    plan = str(final_state.get("trader_investment_plan") or "").strip()
    fallback = str(final_state.get("investment_plan") or "").strip()
    summary = decision or plan or fallback or "无可用结论。"
    summary = " ".join(summary.split())[:1600]
    return (
        "TradingAgents 分析完成\n"
        f"标的: {symbol}\n"
        f"交易日: {trade_date}\n\n"
        f"{summary}"
    )


def _send_webhook(content: str, webhook_url: str) -> bool:
    parsed = urlparse(webhook_url.strip())
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    if "open.feishu.cn" in host and "/open-apis/bot/v2/hook/" in path:
        payload = {"msg_type": "text", "content": {"text": content}}
    elif "qyapi.weixin.qq.com" in host and path == "/cgi-bin/webhook/send":
        payload = {"msgtype": "text", "text": {"content": content}}
    else:
        # Generic fallback for custom webhook receivers.
        payload = {"text": content}

    resp = requests.post(
        webhook_url,
        json=payload,
        headers={"Content-Type": "application/json;charset=utf-8"},
        timeout=15,
    )
    resp.raise_for_status()

    # Feishu/WeCom both return JSON with code/errcode style success fields.
    try:
        body = resp.json()
    except Exception:
        return True
    if "code" in body:
        return int(body.get("code", -1)) == 0
    if "errcode" in body:
        return int(body.get("errcode", -1)) == 0
    return True


def _run_analyst(args: argparse.Namespace) -> int:
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    symbol_raw = args.symbol or args.symbol_positional
    if not symbol_raw:
        raise RuntimeError("请提供股票代码，例如: tradingagents analyst --600519")

    symbol = _normalize_symbol(symbol_raw)
    trade_date = args.trade_date or datetime.now().strftime("%Y-%m-%d")
    query = args.query or f"分析{symbol}"
    webhook_url = (args.webhook_url or os.getenv("FEISHU_WEBHOOK_URL") or "").strip()
    output_file = args.output or ""

    config = _build_runtime_config()
    graph = TradingAgentsGraph(
        selected_analysts=DEFAULT_ANALYSTS,
        debug=bool(args.debug),
        config=config,
    )

    print(f"[CLI] 开始分析: {symbol} @ {trade_date}")
    run_result = asyncio.run(
        graph.propagate_async(
            company_name=symbol,
            trade_date=trade_date,
            query=query,
        )
    )
    short_term = (run_result or {}).get("short_term") or {}
    final_state = short_term
    signal = graph.process_signal(str(short_term.get("final_trade_decision", "")))

    payload = {
        "symbol": symbol,
        "trade_date": trade_date,
        "signal": signal,
        "decision": final_state.get("final_trade_decision", ""),
        "result": final_state,
    }

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[CLI] 分析结果已保存: {output_file}")

    print("\n========== Final Decision ==========")
    print(str(final_state.get("final_trade_decision", "")).strip() or "无最终决策文本")
    print("===================================\n")

    if webhook_url:
        report_text = _build_report_text(symbol, trade_date, final_state)
        ok = _send_webhook(report_text, webhook_url)
        if ok:
            print("[CLI] Webhook 推送成功。")
        else:
            print("[CLI] Webhook 推送失败（返回业务错误码）。")
            return 2

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tradingagents",
        description="TradingAgents CLI",
    )
    subparsers = parser.add_subparsers(dest="command")

    analyst = subparsers.add_parser("analyst", help="运行单标的分析并可选推送 webhook")
    analyst.add_argument("--symbol", type=str, default="", help="股票代码，例如 600519 或 600519.SH")
    analyst.add_argument("symbol_positional", nargs="?", default="", help="股票代码（位置参数）")
    analyst.add_argument("--trade-date", type=str, default="", help="交易日 YYYY-MM-DD，默认今天")
    analyst.add_argument("--query", type=str, default="", help="补充分析意图文本")
    analyst.add_argument("--webhook-url", type=str, default="", help="飞书/企微 webhook URL")
    analyst.add_argument("--output", type=str, default="", help="保存结果到 JSON 文件")
    analyst.add_argument("--debug", action="store_true", help="开启调试输出")

    return parser


def app() -> None:
    _load_dotenv_if_exists(".env")
    argv = _preprocess_argv(os.sys.argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "analyst":
        raise SystemExit(_run_analyst(args))

    parser.print_help()


if __name__ == "__main__":
    app()
