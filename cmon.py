"""cmon — Claude Monitor. Rastreia o consumo do seu plano Claude ao longo do tempo.

Fonte: endpoint privado https://claude.ai/api/oauth/usage (o mesmo que o app usa).
Token, resolvido nesta ordem:
  1. variável de ambiente CLAUDE_OAUTH_TOKEN (útil em CI / override);
  2. cofre seguro do SO — Keychain (macOS), Credential Manager (Windows) ou
     Secret Service (Linux) —, gravado uma vez com `cmon token set`;
  3. credencial do Claude Code, se você estiver logado (zero atrito).
Quando o access token expira, o cmon o renova sozinho via refresh_token e guarda
a nova cadeia no cofre dele (não regrava a credencial do Claude Code). Se um
CLAUDE_OAUTH_TOKEN velho devolver 401, o cmon renova e usa o novo mesmo assim.

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
from datetime import UTC, datetime, timedelta

DB = os.environ.get("CMON_DB", "usage.duckdb")
URL = "https://claude.ai/api/oauth/usage"
# UA obrigatório: sem ele o Cloudflare do claude.ai devolve 403 ("Just a moment").
UA = "claude-cli/1.0 (external, cli)"
LABELS = {"session": "Current session", "weekly_all": "All models"}
SERVICE, ACCOUNT = "cmon", "claude-oauth"  # entrada no cofre seguro do SO (token set manual)
AUTO_ACCOUNT = "claude-oauth-auto"  # cadeia renovada pelo cmon, separada do Claude Code
RETRIES = int(os.environ.get("CMON_RETRIES", "3"))
DEDUP_SECS = int(os.environ.get("CMON_DEDUP_SECS", "60"))  # janela p/ deduplicar collect
# OAuth do Claude Code — valores públicos do fluxo de login; usados só p/ renovar o token.
OAUTH_CLIENT_ID = os.environ.get("CMON_OAUTH_CLIENT_ID", "9d1c250a-e61b-44d9-88ed-5944d1962f5e")
OAUTH_TOKEN_URL = os.environ.get("CMON_OAUTH_TOKEN_URL", "https://console.anthropic.com/v1/oauth/token")


try:  # orjson acelera ~2-3x o parse dos logs; json puro é o fallback.
    import orjson
    _loads = orjson.loads
except ImportError:
    _loads = json.loads


class FetchError(Exception):
    """Falha ao consultar o endpoint de uso, com mensagem já legível ao usuário."""


def _keyring():
    """Módulo keyring, ou None se ausente (dependência opcional em runtime)."""
    try:
        import keyring
        return keyring
    except Exception:
        return None


def _claude_code_cred() -> dict | None:
    """Blob claudeAiOauth do Claude Code, se logado: {accessToken, refreshToken, expiresAt}.
    Cross-platform (arquivo no Linux/Windows, Keychain no macOS)."""
    path = os.path.expanduser("~/.claude/.credentials.json")  # Linux, Windows
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)["claudeAiOauth"]
        except Exception:
            pass
    if sys.platform == "darwin":  # macOS guarda no Keychain, não em arquivo
        try:
            blob = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, check=True).stdout
            return json.loads(blob)["claudeAiOauth"]
        except Exception:
            pass
    return None


def _oauth_refresh(refresh_token: str) -> dict | None:
    """Troca refresh_token por um access_token novo. Best-effort; None se falhar.
    Só fala com o endpoint OAuth da Anthropic — não regrava a credencial do Claude Code."""
    import time

    import requests
    try:
        r = requests.post(OAUTH_TOKEN_URL, timeout=30, json={
            "grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID})
        if r.status_code != 200:
            return None
        d = r.json()
        if not d.get("access_token"):
            return None
        return {"accessToken": d["access_token"],
                "refreshToken": d.get("refresh_token", refresh_token),
                "expiresAt": int((time.time() + int(d.get("expires_in", 3600))) * 1000)}
    except Exception:
        return None


def _auto_load() -> dict | None:
    kr = _keyring()
    if not kr:
        return None
    try:
        raw = kr.get_password(SERVICE, AUTO_ACCOUNT)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _auto_save(blob: dict) -> None:
    kr = _keyring()
    if not kr:
        return
    try:
        kr.set_password(SERVICE, AUTO_ACCOUNT, json.dumps(blob))
    except Exception:
        pass


def _auto_token() -> tuple[str | None, str | None]:
    """(fonte, access_token) da cadeia auto-gerida: prefere a do cmon, senão bootstrapa
    do Claude Code. Renova via refresh_token quando o access expira. (None, None) se nada."""
    import time
    blob, src = _auto_load(), "cmon auto-refresh"
    if not blob:
        blob, src = _claude_code_cred(), "credencial do Claude Code"
    if not blob:
        return None, None
    exp = blob.get("expiresAt") or 0
    if blob.get("accessToken") and time.time() * 1000 < exp - 60_000:  # 60s de folga
        return src, blob["accessToken"]
    if rt := blob.get("refreshToken"):
        if new := _oauth_refresh(rt):
            _auto_save(new)
            return "cmon auto-refresh", new["accessToken"]
    return (src, blob["accessToken"]) if blob.get("accessToken") else (None, None)


def _force_refresh() -> str | None:
    """Força uma renovação (usada quando a API devolve 401). Devolve o novo access_token,
    ou None. Renova a partir da cadeia do cmon ou, na falta, da credencial do Claude Code —
    então funciona mesmo se um CLAUDE_OAUTH_TOKEN velho (env/.env) estiver sombreando tudo."""
    blob = _auto_load() or _claude_code_cred() or {}
    if rt := blob.get("refreshToken"):
        if new := _oauth_refresh(rt):
            _auto_save(new)
            return new["accessToken"]
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
    return _auto_token()


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
    if auto := _auto_load():  # cadeia renovada pelo cmon
        exp = auto.get("expiresAt")
        if exp:
            import time
            rem = (exp / 1000 - time.time()) / 3600
            print(f"Auto  : renovado; expira em {rem:.1f}h" if rem > 0 else "Auto  : expirado (renova no próximo uso)")


def token_clear(_):
    kr = _keyring()
    if not kr:
        sys.exit("Biblioteca 'keyring' ausente. Rode 'uv sync' para instalá-la.")
    n = 0
    for acct in (ACCOUNT, AUTO_ACCOUNT):
        try:
            kr.delete_password(SERVICE, acct)
            n += 1
        except Exception:
            pass
    print(f"Removido do cofre do SO ({n} entrada(s))." if n else "Nada havia guardado no cofre do SO.")


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
    last, override = "?", None
    for attempt in range(retries):
        # override = token recém-renovado após um 401; vence até um CLAUDE_OAUTH_TOKEN velho.
        headers = {"Authorization": f"Bearer {override or get_token()}",
                   "anthropic-beta": "oauth-2025-04-20", "User-Agent": UA}
        try:
            r = requests.get(URL, timeout=30, headers=headers)
            if r.status_code == 401:
                if override is None and (new := _force_refresh()):
                    override = new
                    continue
                raise FetchError("401 — token inválido ou expirado. Abra o Claude Code p/ "
                                 "renovar, defina CLAUDE_OAUTH_TOKEN, ou rode 'cmon token set'.")
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


def deltas(con):
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
    secs = (datetime.fromisoformat(iso) - datetime.now(UTC)).total_seconds()
    if secs < 0:
        return "expirado"
    h, m = divmod(int(secs // 60), 60)
    return f"{h}h {m}min" if h else f"{m}min"


def now(_):
    rows = limits(fetch())
    print("Uso atual:")
    for _k, lbl, pct, reset, _act in rows:
        print(f"  {lbl:16} {bar(pct)} {pct:4.0f}%   reseta em {fmt_eta(reset):>9}")

    sess = next((r for r in rows if r[0] == "session"), None)
    if not sess:
        return
    _k, _lbl, pct, reset, _a = sess
    print(f"\nJanela de 5h: {pct:.0f}% usada — expira em {fmt_eta(reset)}.")

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
    rem_h = (end - datetime.now(UTC)).total_seconds() / 3600
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
    ts = datetime.now(UTC)
    if not getattr(args, "force", False):
        recent = con.execute("SELECT count(*) FROM snapshots WHERE ts > ?",
                             [ts - timedelta(seconds=DEDUP_SECS)]).fetchone()[0]
        if recent:
            print(f"Snapshot há <{DEDUP_SECS}s — pulado (use --force p/ gravar assim mesmo).")
            return
    rows = limits(fetch())
    con.executemany("INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
                    [[ts, k, lbl, pct, reset, act] for k, lbl, pct, reset, act in rows])
    _write_cache(rows, ts)
    print(f"{ts:%Y-%m-%d %H:%M} coletado:")
    for _k, lbl, pct, reset, _a in rows:
        print(f"  {lbl:16} {pct:4.0f}%  reset {reset[:16] if reset else '-'}")
    if getattr(args, "alert", False):
        for m in _alerts(rows, con):
            print(f"⚠ {m}", file=sys.stderr)
            _notify("cmon — limite Claude", m)


def _parse_since(s: str | None):
    """'24h', '7d' ou uma data/hora ISO -> datetime UTC. None se vazio."""
    if not s:
        return None
    s = s.strip().lower()
    now_utc = datetime.now(UTC)
    if s.endswith("h"):
        return now_utc - timedelta(hours=float(s[:-1]))
    if s.endswith("d"):
        return now_utc - timedelta(days=float(s[:-1]))
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def report(args):
    df = deltas(db())
    since = _parse_since(getattr(args, "since", None))
    if since is not None:
        df = df[df.ts >= since]
        if df.empty:
            sys.exit(f"Sem dados desde {since:%Y-%m-%d %H:%M} UTC.")
    g = df.groupby("label").agg(
        snapshots=("percent", "size"),
        pico_pct=("percent", "max"),
        consumo_total_pct=("delta", lambda s: s[s > 0].sum())).round().astype(int)
    if getattr(args, "json", False):
        print(g.reset_index().to_json(orient="records", force_ascii=False))
    else:
        print(g.to_string())


def plot(args):
    try:
        import matplotlib.pyplot as plt
        import seaborn as sns
    except ImportError:
        sys.exit("Os gráficos precisam dos extras de plot. Instale com:\n"
                 "  uv sync --extra plot         (no repositório)\n"
                 "  pip install 'cmon[plot]'     (via PyPI)")
    df = deltas(db())
    df["hora"], df["dia"] = df.ts.dt.hour, df.ts.dt.day_name()
    b = df[df.delta > 0]
    dias = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    sns.set_theme(style="whitegrid")
    fig, ax = plt.subplots(3, 1, figsize=(12, 13))
    sns.lineplot(df, x="ts", y="percent", hue="label", marker="o", ax=ax[0])
    sns.barplot(b, x="hora", y="delta", hue="label", estimator="sum", errorbar=None, ax=ax[1])
    sns.barplot(b, x="dia", y="delta", hue="label", estimator="sum", errorbar=None, order=dias, ax=ax[2])
    titulos = ["Utilização (%) no tempo", "Consumo por hora do dia", "Consumo por dia da semana"]
    for a, t in zip(ax, titulos, strict=True):
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
    now_utc = datetime.now(UTC)
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
        out.append(f"  ↑ upside: ~{100 - proj:.0f}% ficariam na mesa — dá p/ intensificar{extra} "
                   "ou usar modelo mais forte")
    elif proj > 103:
        eta = (100 - pct) / rate
        out.append(f"  ⚠ vai faltar: bate 100% em ~{eta:.0f}h ({rem_h - eta:.0f}h antes do reset) — "
                   f"freie p/ ~{tgt:.2f}%/h ou troque p/ modelo mais barato")
    else:
        out.append("  ✓ no ritmo certo p/ chegar perto de 100% no reset")
    return out, summ


def tips(args):
    rows = limits(fetch())
    con = db()  # writable: _rate + mix de modelos dos logs
    now_utc = datetime.now(UTC)
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

    # mix real de modelos nas últimas 5h (dos logs) — aterra a dica de troca de modelo
    win = now_utc - timedelta(hours=5)
    scan_logs(con, since=win, quiet=True)
    bs = _burn_summary(_burn_rows(con, win))
    if bs:
        tok, cost, mix = bs
        line = f"Mix de modelos (últimas 5h): {tok / 1e6:.2f}M tok · US$ {cost:.2f} · {mix}"
        print(line + "\n")
        summaries.append("- " + line)

    if getattr(args, "no_ai", False):
        return
    tip = _ai_tip("\n".join(summaries))
    if tip:
        print("Dicas (Claude Sonnet):\n" + tip)
    else:
        print("(Dica IA indisponível — 'claude' não encontrado no PATH. Use --no-ai p/ silenciar.)")


def _eta_short(iso: str | None) -> str:
    """fmt_eta compacto p/ statusline: '3h 31min' -> '3h31m'."""
    e = fmt_eta(iso)
    return e.replace(" ", "").replace("min", "m") if e not in ("-", "expirado") else e


CACHE = os.path.expanduser("~/.cmon/status.json")  # fixo: statusline roda de qualquer cwd


def _write_cache(rows, ts) -> None:
    """Grava o último snapshot num cache leve p/ o 'status' ler sem rede/DuckDB."""
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump({"ts": ts.isoformat(), "rows": [list(r) for r in rows]}, f)
    except Exception:
        pass


def _read_cache():
    """(rows, idade_s) do cache JSON, ou None se ausente/ilegível."""
    if not os.path.exists(CACHE):
        return None
    try:
        with open(CACHE, encoding="utf-8") as f:
            d = json.load(f)
        ts = datetime.fromisoformat(d["ts"])
        return [tuple(r) for r in d["rows"]], (datetime.now(UTC) - ts).total_seconds()
    except Exception:
        return None


def _rows_from_db():
    """(rows, idade_s) do último snapshot no banco, ou None se não houver."""
    con = db(create=False)
    if con is None:
        return None
    df = con.execute("SELECT key,label,percent,resets_at,is_active,ts FROM snapshots "
                     "WHERE ts=(SELECT max(ts) FROM snapshots)").df()
    if df.empty:
        return None
    rows = [(r.key, r.label, float(r.percent),
             r.resets_at.isoformat() if r.resets_at is not None else None,
             bool(r.is_active)) for r in df.itertuples()]
    return rows, (datetime.now(UTC) - df.ts.iloc[0]).total_seconds()


def status(args):
    """Linha única p/ statusline/tmux/prompt. Por padrão lê o cache local (rápido,
    ~sub-20ms, sem rede) alimentado pelo 'collect'; --live força a API."""
    age = None
    if args.live:
        try:
            rows = limits(fetch())
        except FetchError:
            print("cmon offline")
            return
    else:
        got = _read_cache() or _rows_from_db()
        if got:
            rows, age = got
        else:
            try:
                rows = limits(fetch())  # sem cache nem banco ainda: cai pra API
            except FetchError:
                print("cmon offline")
                return
    parts = []
    for key, short in (("session", "5h"), ("weekly_all", "sem")):
        r = next((x for x in rows if x[0] == key), None)
        if r:
            parts.append(f"{short} {r[2]:.0f}%")
    sess = next((x for x in rows if x[0] == "session"), None)
    if sess and sess[3]:
        parts.append(f"reset {_eta_short(sess[3])}")
    if age is not None and age > 1800:  # dado velho: sinaliza sem poluir
        parts.append(f"há {age / 60:.0f}m")
    print(" · ".join(parts))


def wait(args):
    """Bloqueia até a janela resetar (padrão) ou atingir --at N%, e notifica.
    Ctrl-C cancela. Poll a cada --interval s; no modo reset dorme até resets_at."""
    import time
    key = args.window

    def get():
        return next((r for r in limits(fetch()) if r[0] == key), None)

    try:
        row = get()
        if not row:
            sys.exit(f"Janela '{key}' não encontrada no endpoint.")
        _k, lbl, pct0, reset, _a = row

        if args.at is not None:
            print(f"Aguardando {lbl} atingir {args.at}% (atual {pct0:.0f}%)… Ctrl-C sai.")
            while True:
                r = get()
                if r and r[2] >= args.at:
                    msg = f"{lbl} atingiu {r[2]:.0f}% (limiar {args.at}%)."
                    print(msg)
                    _notify("cmon — limiar atingido", msg)
                    return
                time.sleep(args.interval)

        if not reset:
            sys.exit(f"{lbl} não tem reset agendado — nada a aguardar.")
        target = datetime.fromisoformat(reset)
        print(f"Aguardando {lbl} resetar (~{fmt_eta(reset)}, {pct0:.0f}% usado agora)… Ctrl-C sai.")
        while True:
            now_utc = datetime.now(UTC)
            if now_utc >= target:
                r = get()
                if r is None or r[2] < pct0 or r[3] != reset:
                    msg = f"{lbl} resetou — pode retomar."
                    print(msg)
                    _notify("cmon — janela liberada", msg)
                    return
                time.sleep(min(args.interval, 30))  # reset ainda não refletido; re-checa
            else:
                time.sleep(min((target - now_utc).total_seconds(), args.interval))
    except KeyboardInterrupt:
        print("\ncancelado.")


def _cycles(con, key: str) -> list:
    """Segmenta os snapshots de uma janela em ciclos, cortando em cada reset
    (queda de percent). Devolve lista de DataFrames (ts, percent)."""
    if con is None:
        return []
    df = con.execute("SELECT ts, percent FROM snapshots WHERE key=? ORDER BY ts", [key]).df()
    if df.empty:
        return []
    segs, start = [], 0
    for i in range(1, len(df)):
        if df.percent.iloc[i] < df.percent.iloc[i - 1]:
            segs.append(df.iloc[start:i])
            start = i
    segs.append(df.iloc[start:])
    return segs


def trends(args):
    """Consumo por ciclo (reset-aware): pico de cada ciclo, delta vs. anterior e
    aviso de anomalia se o ciclo atual destoa da média dos anteriores."""
    con = db(create=False)
    if con is None:
        sys.exit("Sem histórico — rode 'cmon collect' algumas vezes primeiro.")
    for key, lbl in (("weekly_all", "Semanal (All models)"), ("session", "Janela 5h")):
        segs = _cycles(con, key)
        if not segs:
            continue
        peaks = [float(s.percent.max()) for s in segs]
        print(f"\n{lbl} — {len(segs)} ciclo(s):")
        for i in range(max(0, len(segs) - 5), len(segs)):
            ini = segs[i].ts.iloc[0]
            d = peaks[i] - peaks[i - 1] if i > 0 else None
            delta = f"  ({'+' if d >= 0 else ''}{d:.0f} vs anterior)" if d is not None else ""
            print(f"  {ini:%Y-%m-%d %H:%M}  pico {peaks[i]:.0f}%{delta}")
        if len(peaks) >= 3:
            prev = peaks[:-1]
            avg = sum(prev) / len(prev)
            if avg > 0 and peaks[-1] > avg * 1.2:
                print(f"  ⚠ ciclo atual {peaks[-1]:.0f}% vs média {avg:.0f}% (+{peaks[-1] / avg * 100 - 100:.0f}%)")


# --- Camada de logs: minera ~/.claude/projects/**/*.jsonl p/ tokens & custo ---
LOGS_ROOT = os.path.expanduser("~/.claude/projects")
# US$ por milhão de tokens: (input, output, cache_read, cache_write_5m). Estimativa — ajuste aqui.
PRICES = {
    "fable":  (10.0, 50.0, 1.00, 12.50),
    "opus":   (5.0, 25.0, 0.50, 6.25),
    "sonnet": (3.0, 15.0, 0.30, 3.75),
    "haiku":  (1.0, 5.0, 0.10, 1.25),
}


def _price(model: str):
    m = (model or "").lower()
    for k, p in PRICES.items():
        if k in m:
            return p
    return (0.0, 0.0, 0.0, 0.0)


def _short_model(model: str) -> str:
    m = (model or "").lower()
    for k in ("opus", "sonnet", "haiku"):
        if k in m:
            return k.capitalize()
    return model or "?"


SURFACE = {"cli": "terminal", "claude-vscode": "vscode", "claude-desktop": "app (desktop)",
           "sdk-cli": "sdk (-p/agente)"}


def _surface(entrypoint: str) -> str:
    return SURFACE.get(entrypoint, entrypoint or "?")


def _parse_jsonl(path: str):
    """Extrai as mensagens do assistant com usage de um transcript JSONL.
    Só faz json.loads em linhas que contêm '"usage"' — o resto (user/tool) é pulado barato."""
    proj_fallback = os.path.basename(os.path.dirname(path))
    out = []
    try:
        f = open(path, "rb")  # binário + orjson: parse mais rápido
    except OSError:
        return out
    with f:
        for line in f:
            if b'"usage"' not in line:  # pré-filtro: evita parsear a maioria das linhas
                continue
            try:
                o = _loads(line)
            except Exception:
                continue
            msg = o.get("message")
            uid = o.get("uuid")
            if not isinstance(msg, dict) or not uid:
                continue
            u = msg.get("usage")
            ts = o.get("timestamp")
            if not isinstance(u, dict) or not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                continue
            cwd = o.get("cwd")
            proj = os.path.basename(cwd.rstrip("/")) if cwd else proj_fallback
            out.append((uid, dt, msg.get("model") or "?", o.get("entrypoint") or "?",
                        proj, o.get("sessionId") or "?",
                        int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                        int(u.get("cache_read_input_tokens") or 0),
                        int(u.get("cache_creation_input_tokens") or 0)))
    return out


def scan_logs(con, since=None, quiet: bool = False) -> None:
    """Varredura incremental dos JSONL -> tabela token_log. Cacheia por (mtime,size),
    deduplica por uuid; com `since` pula arquivos mais antigos que a janela. O parse
    dos arquivos novos roda em paralelo (multicore)."""
    import glob
    # Migração: se a token_log é de um schema antigo (sem entrypoint), reconstrói — é cache.
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info('token_log')").fetchall()]
    except Exception:
        cols = []  # tabela ainda não existe (banco novo)
    if cols and "entrypoint" not in cols:
        con.execute("DROP TABLE token_log")
        con.execute("DROP TABLE IF EXISTS scanned")
    con.execute("CREATE TABLE IF NOT EXISTS token_log("
                "uuid TEXT PRIMARY KEY, ts TIMESTAMPTZ, model TEXT, entrypoint TEXT, "
                "project TEXT, session TEXT, "
                "in_tok BIGINT, out_tok BIGINT, cache_read BIGINT, cache_create BIGINT)")
    con.execute("CREATE TABLE IF NOT EXISTS scanned(path TEXT PRIMARY KEY, mtime DOUBLE, size BIGINT)")
    seen = {p: (m, s) for p, m, s in con.execute("SELECT path, mtime, size FROM scanned").fetchall()}
    since_epoch = since.timestamp() if since else None
    todo = []
    for f in glob.glob(os.path.join(LOGS_ROOT, "**", "*.jsonl"), recursive=True):
        try:
            st = os.stat(f)
        except OSError:
            continue
        if seen.get(f) == (st.st_mtime, st.st_size):
            continue
        if since_epoch and st.st_mtime < since_epoch:
            continue  # fora da janela; não marca como scanned p/ uma varredura futura pegar
        todo.append((f, st))
    if not todo:
        return
    if not quiet:
        print(f"varrendo {len(todo)} arquivo(s) de log…", file=sys.stderr)
    import pandas as pd
    parsed = _parse_many([f for f, _ in todo])  # paralelo (com fallback serial)
    rows = [r for chunk in parsed for r in chunk]
    if rows:
        # Insert vetorizado via DataFrame: ~680x mais rápido que executemany+ON CONFLICT.
        # drop_duplicates resolve uuids repetidos dentro do lote (transcripts espelhados).
        tdf = pd.DataFrame(rows, columns=["uuid", "ts", "model", "entrypoint", "project",
                                          "session", "in_tok", "out_tok", "cache_read",
                                          "cache_create"]).drop_duplicates("uuid")
        con.register("_tl_new", tdf)
        con.execute("INSERT INTO token_log SELECT * FROM _tl_new ON CONFLICT DO NOTHING")
        con.unregister("_tl_new")
    sdf = pd.DataFrame([[f, st.st_mtime, st.st_size] for f, st in todo],
                       columns=["path", "mtime", "size"])
    con.register("_sc_new", sdf)
    con.execute("INSERT INTO scanned SELECT * FROM _sc_new ON CONFLICT (path) DO UPDATE "
                "SET mtime=excluded.mtime, size=excluded.size")
    con.unregister("_sc_new")


def _parse_many(paths: list) -> list:
    """Parseia vários JSONL em paralelo (ProcessPool). Cai p/ serial se o pool falhar
    ou se forem poucos arquivos (overhead de spawn não compensa)."""
    if len(paths) < 8:
        return [_parse_jsonl(p) for p in paths]
    try:
        from concurrent.futures import ProcessPoolExecutor
        workers = min(8, (os.cpu_count() or 2))
        with ProcessPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(_parse_jsonl, paths, chunksize=16))
    except Exception:
        return [_parse_jsonl(p) for p in paths]


def _burn_rows(con, since):
    """Tokens por (janela, modelo) desde `since`, já com custo estimado por linha."""
    where = "WHERE ts >= ?" if since else ""
    df = con.execute(
        f"SELECT model, sum(in_tok) i, sum(out_tok) o, sum(cache_read) r, sum(cache_create) c "
        f"FROM token_log {where} GROUP BY model", [since] if since else []).df()
    return df


def _burn_summary(df):
    """(tokens_total, custo_usd, 'Opus 80% · Sonnet 20%') a partir de _burn_rows, ou None."""
    if df.empty:
        return None
    tok, cost, bym = 0, 0.0, {}
    for _, r in df.iterrows():
        p = _price(r.model)
        cost += (r.i * p[0] + r.o * p[1] + r.r * p[2] + r.c * p[3]) / 1e6
        t = int(r.i + r.o + r.r + r.c)
        tok += t
        bym[_short_model(r.model)] = bym.get(_short_model(r.model), 0) + t
    mix = " · ".join(f"{k} {v / tok * 100:.0f}%" for k, v in sorted(bym.items(), key=lambda x: -x[1])) if tok else ""
    return tok, cost, mix


def burn(args):
    """Relatório de tokens & US$ estimado a partir dos logs locais do Claude Code."""
    con = db()
    since = _parse_since(getattr(args, "since", None))
    scan_logs(con, since)
    col = {"model": "model", "surface": "entrypoint", "day": "CAST(ts AS DATE)",
           "project": "project", "session": "session"}[args.by]
    where = "WHERE ts >= ?" if since else ""
    df = con.execute(
        f"SELECT {col} grp, model, sum(in_tok) i, sum(out_tok) o, sum(cache_read) r, sum(cache_create) c "
        f"FROM token_log {where} GROUP BY grp, model", [since] if since else []).df()
    if df.empty:
        sys.exit("Sem dados de log do Claude Code no período.")
    df["custo"] = [
        (row.i * _price(row.model)[0] + row.o * _price(row.model)[1]
         + row.r * _price(row.model)[2] + row.c * _price(row.model)[3]) / 1e6
        for row in df.itertuples()]
    df["tok"] = df.i + df.o + df.r + df.c
    g = df.groupby("grp").agg(tok=("tok", "sum"), custo=("custo", "sum")).reset_index()
    g = g.sort_values("custo", ascending=False)
    if getattr(args, "json", False):
        print(g.to_json(orient="records", force_ascii=False))
        return
    per = {"model": "modelo", "surface": "cliente", "day": "dia",
           "project": "projeto", "session": "sessão"}[args.by]
    janela = f" desde {since:%Y-%m-%d %H:%M} UTC" if since else ""
    print(f"Consumo por {per}{janela} (estimado):")
    for row in g.itertuples():
        label = _surface(row.grp) if args.by == "surface" else str(row.grp)
        print(f"  {label:24} {row.tok / 1e6:8.2f}M tok   US$ {row.custo:8.2f}")
    print(f"  {'TOTAL':24} {g.tok.sum() / 1e6:8.2f}M tok   US$ {g.custo.sum():8.2f}")
    print("\n(estimativa; só uso do Claude Code CLI — não inclui claude.ai web/desktop.)")


def watch(args):
    """TUI ao vivo: re-consulta o uso a cada N segundos e redesenha. Ctrl-C sai.
    Com --collect, grava cada leitura no banco (respeitando o dedup)."""
    import time

    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    con = db()  # writable: usado p/ snapshots (--collect), _rate e burn dos logs

    def color(pct: float) -> str:
        return "red" if pct >= 90 else "yellow" if pct >= 70 else "green"

    def render():
        try:
            rows = limits(fetch())
        except FetchError as e:
            return Panel(Text(f"{e}\nnova tentativa em {args.interval}s", style="red"),
                         title="cmon watch — erro", border_style="red")
        if args.collect:
            ts = datetime.now(UTC)
            recent = con.execute("SELECT count(*) FROM snapshots WHERE ts > ?",
                                 [ts - timedelta(seconds=DEDUP_SECS)]).fetchone()[0]
            if not recent:
                con.executemany("INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
                                [[ts, k, lbl, pct, reset, act] for k, lbl, pct, reset, act in rows])
        now_utc = datetime.now(UTC)
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
                      f"{pct:.0f}", fmt_eta(reset),
                      f"{rate:.1f}%/h" if rate else "-", proj)
        body = [t]
        win = now_utc - timedelta(hours=5)
        scan_logs(con, since=win, quiet=True)
        bs = _burn_summary(_burn_rows(con, win))
        if bs:
            tok, cost, mix = bs
            body.append(Text(f"burn 5h (logs): {tok / 1e6:.2f}M tok · US$ {cost:.2f} · {mix}",
                             style="cyan"))
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


AGENT = "com.cmon.collect"  # macOS LaunchAgent / nome-base das units


def _sched_cmd() -> list[str]:
    """Comando que o agendador roda. Interpretador e script absolutos + --db
    absoluto: independe de PATH/cwd/env, que são mínimos em launchd/cron/systemd."""
    return [sys.executable, os.path.abspath(__file__), "--db", os.path.abspath(DB),
            "collect", "--alert"]


def _install_macos(cmd, secs, logdir, dry):
    from xml.sax.saxutils import escape
    plist = os.path.expanduser(f"~/Library/LaunchAgents/{AGENT}.plist")
    args_xml = "".join(f"    <string>{escape(x)}</string>\n" for x in cmd)
    content = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
               '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
               '<plist version="1.0"><dict>\n'
               f'  <key>Label</key><string>{AGENT}</string>\n'
               f'  <key>ProgramArguments</key><array>\n{args_xml}  </array>\n'
               f'  <key>StartInterval</key><integer>{secs}</integer>\n'
               '  <key>RunAtLoad</key><true/>\n'
               f'  <key>StandardOutPath</key><string>{logdir}/collect.out.log</string>\n'
               f'  <key>StandardErrorPath</key><string>{logdir}/collect.err.log</string>\n'
               '</dict></plist>\n')
    if dry:
        print(f"[dry-run] escreveria {plist}:\n{content}[dry-run] launchctl unload/load -w {plist}")
        return
    with open(plist, "w", encoding="utf-8") as f:
        f.write(content)
    subprocess.run(["launchctl", "unload", plist], capture_output=True)
    r = subprocess.run(["launchctl", "load", "-w", plist], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"launchctl load falhou: {r.stderr.strip()}")
    print(f"✓ LaunchAgent instalado: {plist}\n  logs em {logdir}. Desinstalar: cmon uninstall")


def _install_linux(cmd, interval_min, dry):
    import shlex
    import shutil
    exec_line = " ".join(shlex.quote(x) for x in cmd)
    if shutil.which("systemctl"):
        d = os.path.expanduser("~/.config/systemd/user")
        service = (f"[Unit]\nDescription=cmon collect\n\n[Service]\nType=oneshot\n"
                   f"ExecStart={exec_line}\n")
        timer = (f"[Unit]\nDescription=cmon collect timer\n\n[Timer]\nOnBootSec=2min\n"
                 f"OnUnitActiveSec={interval_min}min\nPersistent=true\n\n"
                 f"[Install]\nWantedBy=timers.target\n")
        if dry:
            print(f"[dry-run] {d}/cmon-collect.service:\n{service}\n{d}/cmon-collect.timer:\n{timer}\n"
                  "[dry-run] systemctl --user daemon-reload && enable --now cmon-collect.timer")
            return
        os.makedirs(d, exist_ok=True)
        with open(f"{d}/cmon-collect.service", "w") as f:
            f.write(service)
        with open(f"{d}/cmon-collect.timer", "w") as f:
            f.write(timer)
        subprocess.run(["systemctl", "--user", "daemon-reload"])
        r = subprocess.run(["systemctl", "--user", "enable", "--now", "cmon-collect.timer"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"systemctl falhou: {r.stderr.strip()}")
        print(f"✓ systemd timer instalado ({interval_min}min). Ver: systemctl --user list-timers")
    else:
        _cron(f"*/{interval_min} * * * * {exec_line}", dry)


def _cron(line, dry, remove=False):
    """Adiciona/remove uma linha do crontab marcada com '# cmon'. Fallback sem systemd."""
    tag = "# cmon-collect"
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    kept = [ln for ln in cur.splitlines() if tag not in ln]
    new = kept if remove else kept + [f"{line}  {tag}"]
    if dry:
        print("[dry-run] crontab passaria a ter:\n" + "\n".join(new))
        return
    subprocess.run(["crontab", "-"], input="\n".join(new) + "\n", text=True)
    print("✓ crontab atualizado." if not remove else "✓ entrada removida do crontab.")


def _install_windows(cmd, interval_min, dry):
    tr = " ".join(f'"{x}"' for x in cmd)
    a = ["schtasks", "/create", "/tn", "cmon-collect", "/tr", tr,
         "/sc", "minute", "/mo", str(interval_min), "/f"]
    if dry:
        print("[dry-run] " + subprocess.list2cmdline(a))
        return
    r = subprocess.run(a, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"schtasks falhou: {r.stderr.strip()}")
    print(f"✓ Tarefa 'cmon-collect' agendada ({interval_min}min).")


def install(args):
    """Agenda 'cmon collect --alert' no agendador nativo do SO. --dry-run só mostra."""
    logdir = os.path.expanduser("~/.cmon")
    if not args.dry_run:
        os.makedirs(logdir, exist_ok=True)
    cmd = _sched_cmd()
    m = args.interval
    print(f"Agendando a cada {m}min · banco {os.path.abspath(DB)}\n  {' '.join(cmd)}")
    if sys.platform == "darwin":
        _install_macos(cmd, m * 60, logdir, args.dry_run)
    elif sys.platform.startswith("linux"):
        _install_linux(cmd, m, args.dry_run)
    elif sys.platform.startswith("win"):
        _install_windows(cmd, m, args.dry_run)
    else:
        sys.exit(f"SO não suportado p/ install: {sys.platform}")
    if not args.dry_run:
        print("Token no background: usa o cofre do SO ou a credencial do Claude Code; "
              "'CLAUDE_OAUTH_TOKEN' do shell NÃO é herdado. Rode 'cmon token set' se precisar.")


def uninstall(args):
    """Remove o agendamento criado pelo install."""
    if sys.platform == "darwin":
        plist = os.path.expanduser(f"~/Library/LaunchAgents/{AGENT}.plist")
        subprocess.run(["launchctl", "unload", plist], capture_output=True)
        if os.path.exists(plist):
            os.remove(plist)
        print("✓ LaunchAgent removido.")
    elif sys.platform.startswith("linux"):
        import shutil
        if shutil.which("systemctl"):
            subprocess.run(["systemctl", "--user", "disable", "--now", "cmon-collect.timer"],
                           capture_output=True)
            d = os.path.expanduser("~/.config/systemd/user")
            for n in ("cmon-collect.timer", "cmon-collect.service"):
                p = f"{d}/{n}"
                if os.path.exists(p):
                    os.remove(p)
            subprocess.run(["systemctl", "--user", "daemon-reload"])
            print("✓ systemd timer removido.")
        else:
            _cron("", dry=False, remove=True)
    elif sys.platform.startswith("win"):
        subprocess.run(["schtasks", "/delete", "/tn", "cmon-collect", "/f"], capture_output=True)
        print("✓ Tarefa removida.")
    else:
        sys.exit(f"SO não suportado: {sys.platform}")


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
    p.add_argument("--db", help="caminho do banco DuckDB (sobrepõe CMON_DB)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("now", help="uso atual + tempo até o reset + ritmo/projeção")
    ps = sub.add_parser("status", help="linha única p/ statusline/tmux/prompt (lê cache local)")
    ps.add_argument("--live", action="store_true", help="consulta a API em vez do cache local")
    sub.add_parser("trends", help="consumo por ciclo: pico, delta e anomalia")
    pb = sub.add_parser("burn", help="tokens & US$ estimado dos logs locais do Claude Code")
    pb.add_argument("--by", choices=["model", "surface", "day", "project", "session"], default="model",
                    help="agrupa por modelo (padrão), surface (terminal/vscode/app/sdk), dia, projeto ou sessão")
    pb.add_argument("--since", help="filtra a partir de '24h', '7d' ou data ISO")
    pb.add_argument("--json", action="store_true", help="saída em JSON")
    pc = sub.add_parser("collect", help="grava 1 snapshot no banco")
    pc.add_argument("--force", action="store_true", help="grava mesmo com snapshot recente (ignora dedup)")
    pc.add_argument("--alert", action="store_true",
                    help="avisa (stderr + notificação) se projetar 100%% antes do reset")
    pr = sub.add_parser("report", help="resumo do consumo acumulado")
    pr.add_argument("--since", help="filtra a partir de '24h', '7d' ou data ISO")
    pr.add_argument("--json", action="store_true", help="saída em JSON")
    pw = sub.add_parser("watch", help="TUI ao vivo com o uso atual (Ctrl-C sai)")
    pw.add_argument("-n", "--interval", type=int, default=30, help="segundos entre atualizações (padrão 30)")
    pw.add_argument("--collect", action="store_true", help="grava cada leitura no banco enquanto observa")
    pwa = sub.add_parser("wait", help="bloqueia até a janela resetar (ou --at N%%), então notifica")
    pwa.add_argument("--window", default="session", help="janela a observar (padrão session = 5h; ex.: weekly_all)")
    pwa.add_argument("--at", type=float, help="em vez do reset, aguarda o uso atingir N%%")
    pwa.add_argument("-n", "--interval", type=int, default=60, help="segundos entre verificações (padrão 60)")
    pin = sub.add_parser("install", help="agenda 'collect --alert' no agendador do SO (coleta de fundo)")
    pin.add_argument("-i", "--interval", type=int, default=20, help="minutos entre coletas (padrão 20)")
    pin.add_argument("--dry-run", action="store_true", help="mostra o que faria, sem instalar")
    sub.add_parser("uninstall", help="remove o agendamento criado pelo install")
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
    if args.db:
        global DB
        DB = args.db
    if args.cmd == "token":
        {"set": token_set, "status": token_status, "clear": token_clear}[args.action](args)
        return
    try:
        {"now": now, "status": status, "trends": trends, "collect": collect, "report": report,
         "burn": burn, "watch": watch, "wait": wait, "plot": plot, "tips": tips,
         "install": install, "uninstall": uninstall}[args.cmd](args)
    except FetchError as e:
        sys.exit(f"cmon: {e}")


if __name__ == "__main__":
    main()
