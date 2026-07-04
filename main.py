"""cmon — Claude Monitor. Rastreia o consumo do seu plano Claude ao longo do tempo.

Fonte: endpoint privado https://claude.ai/api/oauth/usage (o mesmo que o app usa).
Token: lido do Keychain do macOS (login do Claude Code, renovado automaticamente)
       ou da variável de ambiente CLAUDE_OAUTH_TOKEN.

  cmon now       # uso atual + tempo até o reset + ritmo/projeção
  cmon collect   # grava 1 snapshot no banco (rode via cron a cada ~20min)
  cmon report    # resumo do consumo acumulado
  cmon plot      # gráficos seaborn -> PNG
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

DB = os.environ.get("CMON_DB", "usage.duckdb")
URL = "https://claude.ai/api/oauth/usage"
# UA obrigatório: sem ele o Cloudflare do claude.ai devolve 403 ("Just a moment").
UA = "claude-cli/1.0 (external, cli)"
LABELS = {"session": "Current session", "weekly_all": "All models"}


def get_token() -> str:
    if tok := os.environ.get("CLAUDE_OAUTH_TOKEN"):
        return tok
    try:  # macOS: credencial que o Claude Code guarda e mantém válida
        blob = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, check=True).stdout
        return json.loads(blob)["claudeAiOauth"]["accessToken"]
    except Exception:
        sys.exit("Sem token: defina CLAUDE_OAUTH_TOKEN ou faça login no Claude Code (macOS).")


def fetch() -> dict:
    import requests
    r = requests.get(URL, timeout=30, headers={
        "Authorization": f"Bearer {get_token()}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": UA})
    r.raise_for_status()
    return r.json()


def limits(data: dict) -> list[tuple]:
    """Normaliza limits[] -> (key, label, percent, resets_at, is_active). key é estável p/ delta."""
    out = []
    for lim in data.get("limits", []):
        kind = lim["kind"]
        model = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
        key = f"{kind}:{model}" if model else kind
        label = LABELS.get(kind) or (f"{model} only" if model else kind)
        out.append((key, label, float(lim["percent"]), lim.get("resets_at"), lim.get("is_active")))
    return out


def db(create: bool = True):
    """Conexão DuckDB. create=False retorna None se o banco ainda não existe (não cria arquivo)."""
    if not create and not os.path.exists(DB):
        return None
    import duckdb
    con = duckdb.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS snapshots("
                "ts TIMESTAMPTZ, key TEXT, label TEXT, percent DOUBLE, "
                "resets_at TIMESTAMPTZ, is_active BOOL)")
    return con


def burn(con):
    """delta = percent - snapshot anterior da mesma janela; delta<0 = reset (não é consumo)."""
    df = con.execute(
        "SELECT ts, key, label, percent, percent - lag(percent) "
        "OVER (PARTITION BY key ORDER BY ts) AS delta "
        "FROM snapshots ORDER BY ts").df()
    if df.empty:
        sys.exit("Sem dados — rode 'cmon collect' algumas vezes primeiro.")
    return df


def bar(pct: float, width: int = 20) -> str:
    fill = int(min(pct, 100) / 100 * width)
    return "█" * fill + "·" * (width - fill)


def fmt_eta(iso: str | None) -> str:
    if not iso:
        return "-"
    secs = (datetime.fromisoformat(iso) - datetime.now(timezone.utc)).total_seconds()
    if secs < 0:
        return "expirado"
    h, m = divmod(int(secs // 60), 60)
    return f"{h}h {m}min" if h else f"{m}min"


def now(_):
    rows = limits(fetch())
    print("Uso atual:")
    for _k, lbl, pct, reset, act in rows:
        flag = "  ←ativo" if act else ""
        print(f"  {lbl:16} {bar(pct)} {pct:5.1f}%   reseta em {fmt_eta(reset):>9}{flag}")

    sess = next((r for r in rows if r[0] == "session"), None)
    if not sess:
        return
    _k, _lbl, pct, reset, _a = sess
    print(f"\nJanela de 5h: {pct:.1f}% usada — expira em {fmt_eta(reset)}.")

    con = db(create=False)
    if con is None:
        return
    end = datetime.fromisoformat(reset)
    win = con.execute(
        "SELECT ts, percent FROM snapshots WHERE key='session' AND ts >= ? ORDER BY ts",
        [end - timedelta(hours=5)]).df()
    if len(win) < 2:
        return
    dt_h = (win.ts.iloc[-1] - win.ts.iloc[0]).total_seconds() / 3600
    dpct = win.percent.iloc[-1] - win.percent.iloc[0]
    if dt_h <= 0 or dpct <= 0:
        print("Ritmo: sem consumo mensurável nesta janela.")
        return
    rate = dpct / dt_h
    rem_h = (end - datetime.now(timezone.utc)).total_seconds() / 3600
    proj = min(pct + rate * rem_h, 100)
    print(f"Ritmo: {rate:.1f}%/h → projeção no reset: {proj:.0f}%.")
    if pct < 100:
        to100 = (100 - pct) / rate
        if to100 < rem_h:
            print(f"⚠ No ritmo atual você atinge 100% em ~{to100:.1f}h, antes do reset.")


def collect(_):
    rows = limits(fetch())
    ts = datetime.now(timezone.utc)
    con = db()
    con.executemany("INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
                    [[ts, k, lbl, pct, reset, act] for k, lbl, pct, reset, act in rows])
    print(f"{ts:%Y-%m-%d %H:%M} coletado:")
    for _k, lbl, pct, reset, _a in rows:
        print(f"  {lbl:16} {pct:5.1f}%  reset {reset[:16] if reset else '-'}")


def report(_):
    g = burn(db()).groupby("label").agg(
        snapshots=("percent", "size"),
        pico_pct=("percent", "max"),
        consumo_total_pct=("delta", lambda s: s[s > 0].sum())).round(1)
    print(g.to_string())


def plot(args):
    import matplotlib.pyplot as plt
    import seaborn as sns
    df = burn(db())
    df["hora"], df["dia"] = df.ts.dt.hour, df.ts.dt.day_name()
    b = df[df.delta > 0]
    dias = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    sns.set_theme(style="whitegrid")
    fig, ax = plt.subplots(3, 1, figsize=(12, 13))
    sns.lineplot(df, x="ts", y="percent", hue="label", marker="o", ax=ax[0])
    sns.barplot(b, x="hora", y="delta", hue="label", estimator="sum", errorbar=None, ax=ax[1])
    sns.barplot(b, x="dia", y="delta", hue="label", estimator="sum", errorbar=None, order=dias, ax=ax[2])
    for a, t in zip(ax, ["Utilização (%) no tempo", "Consumo por hora do dia", "Consumo por dia da semana"]):
        a.set_title(t)
        a.set_xlabel("")
    fig.tight_layout()
    fig.savefig(args.out, dpi=150)
    print(f"{args.out} salvo")


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    p = argparse.ArgumentParser(prog="cmon", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("now", help="uso atual + tempo até o reset + ritmo/projeção")
    sub.add_parser("collect", help="grava 1 snapshot no banco")
    sub.add_parser("report", help="resumo do consumo acumulado")
    pp = sub.add_parser("plot", help="gera gráficos -> PNG")
    pp.add_argument("-o", "--out", default="usage.png")
    args = p.parse_args()
    {"now": now, "collect": collect, "report": report, "plot": plot}[args.cmd](args)


if __name__ == "__main__":
    main()
