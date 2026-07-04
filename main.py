"""cmon — Claude Monitor. Rastreia o consumo do seu plano Claude ao longo do tempo.

Fonte: endpoint privado https://claude.ai/api/oauth/usage (o mesmo que o app usa).
Token, resolvido nesta ordem:
  1. variável de ambiente CLAUDE_OAUTH_TOKEN (útil em CI / override);
  2. cofre seguro do SO — Keychain (macOS), Credential Manager (Windows) ou
     Secret Service (Linux) —, gravado uma vez com `cmon token set`;
  3. credencial do Claude Code, se você estiver logado (zero atrito).

  cmon now         # uso atual + tempo até o reset + ritmo/projeção
  cmon collect     # grava 1 snapshot no banco (rode via cron a cada ~20min)
  cmon report      # resumo do consumo acumulado
  cmon plot        # gráficos seaborn -> PNG
  cmon tips        # dicas de pacing p/ usar ~100% do semanal sem travar o 5h
  cmon token set   # guarda o token no cofre seguro do SO (cross-platform)
"""

import argparse
import getpass
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
SERVICE, ACCOUNT = "cmon", "claude-oauth"  # entrada no cofre seguro do SO
RETRIES = int(os.environ.get("CMON_RETRIES", "3"))
DEDUP_SECS = int(os.environ.get("CMON_DEDUP_SECS", "60"))  # janela p/ deduplicar collect


class FetchError(Exception):
    """Falha ao consultar o endpoint de uso, com mensagem já legível ao usuário."""


def _keyring():
    """Módulo keyring, ou None se ausente (dependência opcional em runtime)."""
    try:
        import keyring
        return keyring
    except Exception:
        return None


def _claude_code_token() -> str | None:
    """Credencial nativa do Claude Code, se logado. Zero atrito, cross-platform."""
    path = os.path.expanduser("~/.claude/.credentials.json")  # Linux, Windows
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)["claudeAiOauth"]["accessToken"]
        except Exception:
            pass
    if sys.platform == "darwin":  # macOS guarda no Keychain, não em arquivo
        try:
            blob = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, check=True).stdout
            return json.loads(blob)["claudeAiOauth"]["accessToken"]
        except Exception:
            pass
    return None


def _resolve_token() -> tuple[str | None, str | None]:
    """(fonte, token) na ordem de precedência; (None, None) se nada for achado."""
    if tok := os.environ.get("CLAUDE_OAUTH_TOKEN"):
        return "env CLAUDE_OAUTH_TOKEN", tok
    if kr := _keyring():
        try:
            if tok := kr.get_password(SERVICE, ACCOUNT):
                return f"cofre do SO ({kr.get_keyring().name})", tok
        except Exception:
            pass
    if tok := _claude_code_token():
        return "credencial do Claude Code", tok
    return None, None


def get_token() -> str:
    _src, tok = _resolve_token()
    if not tok:
        sys.exit("Sem token. Rode 'cmon token set' para guardá-lo com segurança, "
                 "defina CLAUDE_OAUTH_TOKEN, ou faça login no Claude Code.")
    return tok


def _mask(tok: str) -> str:
    """Nunca imprime o token inteiro: só o prefixo e o sufixo."""
    return f"{tok[:12]}…{tok[-4:]}" if len(tok) > 20 else "…"


def token_set(_):
    kr = _keyring()
    if not kr:
        sys.exit("Biblioteca 'keyring' ausente. Rode 'uv sync' para instalá-la.")
    # stdin isatty → prompt oculto; senão lê de pipe (ex.: echo $TOK | cmon token set).
    tok = (getpass.getpass("Cole o token OAuth (oculto): ") if sys.stdin.isatty()
           else sys.stdin.readline()).strip()
    if not tok:
        sys.exit("Token vazio — nada foi guardado.")
    try:
        kr.set_password(SERVICE, ACCOUNT, tok)
    except Exception as e:
        sys.exit(f"Falha ao acessar o cofre do SO: {e}\n"
                 "Em Linux headless instale um backend (ex.: gnome-keyring) "
                 "ou use CLAUDE_OAUTH_TOKEN.")
    print(f"✓ Token guardado no cofre do SO ({kr.get_keyring().name}).")


def token_status(_):
    src, tok = _resolve_token()
    if not tok:
        print("Nenhum token disponível. Rode 'cmon token set'.")
        return
    print(f"Fonte : {src}\nToken : {_mask(tok)}")


def token_clear(_):
    kr = _keyring()
    if not kr:
        sys.exit("Biblioteca 'keyring' ausente. Rode 'uv sync' para instalá-la.")
    try:
        kr.delete_password(SERVICE, ACCOUNT)
        print("Token removido do cofre do SO.")
    except Exception:
        print("Nada havia guardado no cofre do SO.")


def _retry_after(r) -> float | None:
    """Segundos pedidos pelo header Retry-After (429/503), se numérico."""
    try:
        return float(r.headers.get("Retry-After") or "")
    except ValueError:
        return None


def fetch(retries: int = RETRIES) -> dict:
    """Consulta o endpoint de uso com retry/backoff. Levanta FetchError com
    mensagem legível — 401/403 falham na hora (não adianta repetir); 429 e 5xx
    e erros de rede tentam de novo com espera exponencial (respeitando Retry-After)."""
    import time
    import requests
    headers = {"Authorization": f"Bearer {get_token()}",
               "anthropic-beta": "oauth-2025-04-20", "User-Agent": UA}
    last = "?"
    for attempt in range(retries):
        try:
            r = requests.get(URL, timeout=30, headers=headers)
            if r.status_code == 401:
                raise FetchError("401 — token inválido ou expirado. Faça login no "
                                 "Claude Code ou rode 'cmon token set'.")
            if r.status_code == 403:
                raise FetchError("403 — bloqueado (Cloudflare/User-Agent) ou sem "
                                 "acesso. O endpoint privado pode ter mudado.")
            if r.status_code == 429 or r.status_code >= 500:
                last, wait = f"HTTP {r.status_code}", _retry_after(r) or 2 ** attempt
            else:
                r.raise_for_status()
                return r.json()
        except requests.RequestException as e:
            last, wait = f"rede: {e}", 2 ** attempt
        if attempt < retries - 1:
            time.sleep(wait)
    raise FetchError(f"Falha ao consultar {URL} após {retries} tentativas ({last}).")


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
    for _k, lbl, pct, reset, _act in rows:
        print(f"  {lbl:16} {bar(pct)} {pct:5.1f}%   reseta em {fmt_eta(reset):>9}")

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

    for m in _alerts(rows, con):
        if not m.startswith("Current session"):  # 5h já coberta acima
            print(f"⚠ {m}")


def collect(args):
    con = db()
    ts = datetime.now(timezone.utc)
    if not getattr(args, "force", False):
        recent = con.execute("SELECT count(*) FROM snapshots WHERE ts > ?",
                             [ts - timedelta(seconds=DEDUP_SECS)]).fetchone()[0]
        if recent:
            print(f"Snapshot há <{DEDUP_SECS}s — pulado (use --force p/ gravar assim mesmo).")
            return
    rows = limits(fetch())
    con.executemany("INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
                    [[ts, k, lbl, pct, reset, act] for k, lbl, pct, reset, act in rows])
    print(f"{ts:%Y-%m-%d %H:%M} coletado:")
    for _k, lbl, pct, reset, _a in rows:
        print(f"  {lbl:16} {pct:5.1f}%  reset {reset[:16] if reset else '-'}")
    if getattr(args, "alert", False):
        for m in _alerts(rows, con):
            print(f"⚠ {m}", file=sys.stderr)
            _notify("cmon — limite Claude", m)


def _parse_since(s: str | None):
    """'24h', '7d' ou uma data/hora ISO -> datetime UTC. None se vazio."""
    if not s:
        return None
    s = s.strip().lower()
    now_utc = datetime.now(timezone.utc)
    if s.endswith("h"):
        return now_utc - timedelta(hours=float(s[:-1]))
    if s.endswith("d"):
        return now_utc - timedelta(days=float(s[:-1]))
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def report(args):
    df = burn(db())
    since = _parse_since(getattr(args, "since", None))
    if since is not None:
        df = df[df.ts >= since]
        if df.empty:
            sys.exit(f"Sem dados desde {since:%Y-%m-%d %H:%M} UTC.")
    g = df.groupby("label").agg(
        snapshots=("percent", "size"),
        pico_pct=("percent", "max"),
        consumo_total_pct=("delta", lambda s: s[s > 0].sum())).round(1)
    if getattr(args, "json", False):
        print(g.reset_index().to_json(orient="records", force_ascii=False))
    else:
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


def _rate(con, key) -> float | None:
    """%/h observado na janela atual. Corta no último reset (queda de percent),
    então adapta-se sozinho a janelas de 5h, 7d ou o que a API usar."""
    if con is None:
        return None
    df = con.execute("SELECT ts, percent FROM snapshots WHERE key=? ORDER BY ts", [key]).df()
    if len(df) < 2:
        return None
    drops = [i for i in range(1, len(df)) if df.percent.iloc[i] < df.percent.iloc[i - 1]]
    seg = df.iloc[drops[-1]:] if drops else df  # só o trecho depois do último reset
    if len(seg) < 2:
        return None
    dt_h = (seg.ts.iloc[-1] - seg.ts.iloc[0]).total_seconds() / 3600
    dpct = seg.percent.iloc[-1] - seg.percent.iloc[0]
    return dpct / dt_h if dt_h > 0 and dpct > 0 else None


def _alerts(rows, con) -> list[str]:
    """Avisa quando, no ritmo atual, uma janela bate 100% antes do reset.
    Precisa de histórico (via _rate); sem banco/ritmo não gera nada."""
    now_utc = datetime.now(timezone.utc)
    msgs = []
    for key, lbl, pct, reset, _a in rows:
        if not reset or pct >= 100:
            continue
        rate = _rate(con, key)
        if not rate:
            continue
        rem_h = (datetime.fromisoformat(reset) - now_utc).total_seconds() / 3600
        if rem_h <= 0:
            continue
        eta = (100 - pct) / rate
        if eta < rem_h:
            msgs.append(f"{lbl}: no ritmo de {rate:.1f}%/h bate 100% em ~{eta:.1f}h "
                        f"(~{rem_h - eta:.1f}h antes do reset).")
    return msgs


def _notify(title: str, body: str) -> None:
    """Notificação nativa best-effort (osascript no macOS, notify-send no Linux).
    Silenciosa se a ferramenta não existir — nunca derruba o comando."""
    import shutil
    try:
        if sys.platform == "darwin":
            subprocess.run(["osascript", "-e",
                            f"display notification {json.dumps(body)} "
                            f"with title {json.dumps(title)}"],
                           capture_output=True, timeout=10)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, body], capture_output=True, timeout=10)
    except Exception:
        pass


def _ai_tip(summary: str) -> str | None:
    """Passa os números pro Claude (sonnet, barato) e devolve 3 dicas. None se indisponível."""
    import shutil
    if not shutil.which("claude"):
        return None
    prompt = (
        "Você otimiza o uso de um plano Claude. Estado atual:\n" + summary +
        "\n\nObjetivo do usuário: chegar perto de 100% do limite SEMANAL no reset — "
        "sem esgotar antes e sem estourar a janela de 5h (que trava o uso).\n"
        "Dê no máximo 3 dicas de 1 linha, acionáveis, priorizando: pacing "
        "(acelerar se sobra folga / frear se vai faltar), troca de modelo (Haiku "
        "barato p/ tarefas simples, Sonnet p/ código, Opus só p/ difícil) e horário "
        "(pico 8h–14h ET nos dias úteis drena o 5h mais rápido). Sem preâmbulo, sem "
        "markdown. Use SÓ os números acima — não invente ritmos ou projeções ausentes."
    )
    try:
        r = subprocess.run(["claude", "-p", prompt, "--model", "sonnet"],
                           capture_output=True, text=True, timeout=120)
        return r.stdout.strip() or None
    except Exception:
        return None


def _window_tips(lbl: str, pct: float, reset: str | None, rate: float | None, now_utc):
    """Bloco determinístico de pacing p/ uma janela. Devolve (linhas_impressas, linha_resumo)."""
    if not reset:
        return [f"{lbl}: {pct:.0f}% usado."], f"- {lbl}: {pct:.0f}% usado"
    rem_h = (datetime.fromisoformat(reset) - now_utc).total_seconds() / 3600
    if rem_h <= 0:
        return [f"{lbl}: {pct:.0f}% usado, resetando agora."], f"- {lbl}: {pct:.0f}%, reset iminente"
    tgt = (100 - pct) / rem_h  # %/h p/ chegar exatamente a 100 no reset
    out = [f"{lbl}: {pct:.0f}% usado · reseta em {fmt_eta(reset)} · alvo p/ zerar folga {tgt:.2f}%/h"]
    summ = f"- {lbl}: {pct:.0f}% usado, reseta em {fmt_eta(reset)}, alvo {tgt:.2f}%/h"
    if rate is None:
        out.append("  (sem ritmo ainda — rode 'cmon collect' mais vezes)")
        return out, summ
    proj = pct + rate * rem_h
    summ += f", ritmo {rate:.2f}%/h, projeção {min(proj, 999):.0f}%"
    out.append(f"  ritmo atual {rate:.2f}%/h → projeção no reset ~{min(proj, 999):.0f}%")
    if proj < 97:
        gap = tgt - rate
        extra = f" (~+{gap:.2f}%/h)" if gap > 0 else ""
        out.append(f"  ↑ upside: ~{100 - proj:.0f}% ficariam na mesa — dá p/ intensificar{extra} ou usar modelo mais forte")
    elif proj > 103:
        eta = (100 - pct) / rate
        out.append(f"  ⚠ vai faltar: bate 100% em ~{eta:.0f}h ({rem_h - eta:.0f}h antes do reset) — freie p/ ~{tgt:.2f}%/h ou troque p/ modelo mais barato")
    else:
        out.append("  ✓ no ritmo certo p/ chegar perto de 100% no reset")
    return out, summ


def tips(args):
    rows = limits(fetch())
    con = db(create=False)
    now_utc = datetime.now(timezone.utc)
    print("cmon tips — usar ~100% do semanal sem faltar nem travar a janela de 5h.\n")

    summaries = []
    for key, want in (("weekly_all", "Semanal (All models)"), ("session", "Janela 5h")):
        row = next((r for r in rows if r[0] == key), None)
        if not row:
            continue
        _k, _lbl, pct, reset, _a = row
        block, summ = _window_tips(want, pct, reset, _rate(con, key), now_utc)
        print("\n".join(block) + "\n")
        summaries.append(summ)

    # modelos escopados (ex.: Fable/Opus) entram só no resumo pra IA sugerir troca
    for key, lbl, pct, reset, _a in rows:
        if key not in ("weekly_all", "session"):
            summaries.append(f"- {lbl}: {pct:.0f}% usado, reseta em {fmt_eta(reset)}")

    if getattr(args, "no_ai", False):
        return
    tip = _ai_tip("\n".join(summaries))
    if tip:
        print("Dicas (Claude Sonnet):\n" + tip)
    else:
        print("(Dica IA indisponível — 'claude' não encontrado no PATH. Use --no-ai p/ silenciar.)")


def watch(args):
    """TUI ao vivo: re-consulta o uso a cada N segundos e redesenha. Ctrl-C sai.
    Com --collect, grava cada leitura no banco (respeitando o dedup)."""
    import time
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    con = db() if args.collect else db(create=False)

    def color(pct: float) -> str:
        return "red" if pct >= 90 else "yellow" if pct >= 70 else "green"

    def render():
        try:
            rows = limits(fetch())
        except FetchError as e:
            return Panel(Text(f"{e}\nnova tentativa em {args.interval}s", style="red"),
                         title="cmon watch — erro", border_style="red")
        if args.collect:
            ts = datetime.now(timezone.utc)
            recent = con.execute("SELECT count(*) FROM snapshots WHERE ts > ?",
                                 [ts - timedelta(seconds=DEDUP_SECS)]).fetchone()[0]
            if not recent:
                con.executemany("INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
                                [[ts, k, lbl, pct, reset, act] for k, lbl, pct, reset, act in rows])
        now_utc = datetime.now(timezone.utc)
        t = Table(expand=True, header_style="bold")
        t.add_column("Janela")
        t.add_column("Uso", ratio=1)
        t.add_column("%", justify="right")
        t.add_column("reseta em", justify="right")
        t.add_column("ritmo", justify="right")
        t.add_column("projeção", justify="right")
        for key, lbl, pct, reset, act in rows:
            rate = _rate(con, key)
            rem_h = ((datetime.fromisoformat(reset) - now_utc).total_seconds() / 3600
                     if reset else None)
            proj = f"{min(pct + rate * rem_h, 999):.0f}%" if rate and rem_h and rem_h > 0 else "-"
            t.add_row(lbl + (" ●" if act else ""),
                      Text(bar(pct, 28), style=color(pct)),
                      f"{pct:.1f}", fmt_eta(reset),
                      f"{rate:.1f}%/h" if rate else "-", proj)
        body = [t]
        alerts = _alerts(rows, con)
        if alerts:
            body.append(Text("\n".join("⚠ " + m for m in alerts), style="bold red"))
        body.append(Text(f"{now_utc:%H:%M:%S} UTC · atualiza a cada {args.interval}s"
                         f"{' · gravando' if args.collect else ''} · Ctrl-C sai", style="dim"))
        return Panel(Group(*body), title="cmon watch", border_style="cyan")

    with Live(render(), console=Console(), screen=True, refresh_per_second=4) as live:
        try:
            while True:
                time.sleep(args.interval)
                live.update(render())
        except KeyboardInterrupt:
            pass


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    p = argparse.ArgumentParser(
        prog="cmon", description=__doc__.splitlines()[0],
        epilog="Token: env CLAUDE_OAUTH_TOKEN → cofre do SO (cmon token set) → "
               "credencial do Claude Code. Veja 'cmon token --help'.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("now", help="uso atual + tempo até o reset + ritmo/projeção")
    pc = sub.add_parser("collect", help="grava 1 snapshot no banco")
    pc.add_argument("--force", action="store_true", help="grava mesmo com snapshot recente (ignora dedup)")
    pc.add_argument("--alert", action="store_true", help="avisa (stderr + notificação) se projetar 100%% antes do reset")
    pr = sub.add_parser("report", help="resumo do consumo acumulado")
    pr.add_argument("--since", help="filtra a partir de '24h', '7d' ou data ISO")
    pr.add_argument("--json", action="store_true", help="saída em JSON")
    pw = sub.add_parser("watch", help="TUI ao vivo com o uso atual (Ctrl-C sai)")
    pw.add_argument("-n", "--interval", type=int, default=30, help="segundos entre atualizações (padrão 30)")
    pw.add_argument("--collect", action="store_true", help="grava cada leitura no banco enquanto observa")
    pp = sub.add_parser("plot", help="gera gráficos -> PNG")
    pp.add_argument("-o", "--out", default="usage.png")

    pd_ = sub.add_parser("tips", help="dicas de pacing p/ usar ~100%% do semanal sem travar o 5h",
                         description="Projeta o consumo por janela e sugere acelerar/frear/trocar "
                                     "de modelo. Enriquece com Claude Sonnet via 'claude -p'.")
    pd_.add_argument("--no-ai", action="store_true", help="só as projeções locais, sem chamar o Claude")

    pt = sub.add_parser("token", help="gerencia o token OAuth com segurança (cross-platform)",
                        description="Guarda o token no cofre nativo do SO (Keychain no macOS, "
                                    "Credential Manager no Windows, Secret Service no Linux).")
    ta = pt.add_subparsers(dest="action", required=True)
    ta.add_parser("set", help="guarda um token no cofre do SO (input oculto; aceita pipe)")
    ta.add_parser("status", help="mostra de onde vem o token, mascarado")
    ta.add_parser("clear", help="remove o token guardado no cofre")

    args = p.parse_args()
    if args.cmd == "token":
        {"set": token_set, "status": token_status, "clear": token_clear}[args.action](args)
        return
    try:
        {"now": now, "collect": collect, "report": report,
         "watch": watch, "plot": plot, "tips": tips}[args.cmd](args)
    except FetchError as e:
        sys.exit(f"cmon: {e}")


if __name__ == "__main__":
    main()
