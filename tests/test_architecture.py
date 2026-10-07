"""Structural guarantees: no look-ahead leakage, no LLM in the exit path."""
import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "tradebot"
TRADING_PATH = ["risk.py", "protection.py", "execution.py", "bot.py", "circuit_breaker.py", "portfolio.py"]


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            out.add((n.module or "") if n.level == 0 else "." + (n.module or ""))
    return out


def test_markouts_never_feed_trading_decisions():
    for f in TRADING_PATH:
        assert not any("markout" in m for m in imports(PKG / f)), f


def test_no_llm_or_network_in_time_critical_path():
    banned = ("anthropic", "openai", "requests", "httpx", "urllib", "aiohttp", "data.birdeye")
    for f in TRADING_PATH:
        for m in imports(PKG / f):
            assert not any(b in m for b in banned), (f, m)


def test_no_floats_in_money_paths():
    for f in ["risk.py", "protection.py", "portfolio.py", "execution.py"]:
        src = (PKG / f).read_text()
        assert "float(" not in src, f
