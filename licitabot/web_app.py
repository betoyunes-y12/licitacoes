"""Servidor HTTP da interface — apenas biblioteca padrão.

POR QUE SEM FRAMEWORK
---------------------
O projeto inteiro não tem dependência externa, e isso é uma vantagem concreta:
instalar é `git clone` e rodar. Trazer Flask/FastAPI trocaria isso por um
`pip install` e um requirements.txt para manter. Para uma API de leitura sobre
SQLite, `http.server` dá conta.

O que o servidor faz:
    GET /                       -> interface de licitações
    GET /resultados             -> interface de resultados (contratos)
    GET /api/licitacoes         -> JSON paginado com filtros
    GET /api/contratos          -> JSON paginado de contratos
    GET /api/facetas            -> contagens para os filtros
    GET /api/estatisticas       -> números do topo
    GET /api/exportar.csv       -> download do resultado filtrado

DECISÕES DE SEGURANÇA
---------------------
- SQLite é aberto em modo somente-leitura (`mode=ro`): a interface não pode
  alterar a base por acidente, mesmo com bug na camada web.
- CORS liberado apenas para GET, e o servidor escuta em 127.0.0.1 por padrão.
  Para expor, use um proxy reverso com autenticação — a base tem dado
  comercial e não deve ficar aberta.
- Nenhum parâmetro do cliente entra em SQL (ver web_queries.py).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .store import DB_PATH
from .web_queries import (
    consultar_paginado,
    estatisticas_gerais,
    facetas,
)

WEB_DIR = Path(__file__).resolve().parent / "web"

TIPOS = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def abrir_ro(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Abre a base em modo SOMENTE LEITURA.

    A interface web nunca precisa escrever. Abrir read-only transforma um bug
    de programação de "corrompeu a base" em "deu erro na consulta".
    """
    p = Path(db_path) if db_path else DB_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"base não encontrada em {p}. Rode uma coleta primeiro:\n"
            f"  python3 -m licitabot.cli ingerir --abertos"
        )
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=15,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# Faixas autorizadas por padrão: localhost e a rede privada do Tailscale
# (100.64.0.0/10, CGNAT). Isso permite usar a interface pelo tailnet — que é
# privado — sem abrir para a internet. Qualquer outra origem é recusada.
FAIXAS_PERMITIDAS = ("127.", "::1", "100.", "fd7a:115c:a1e0:")


class Handler(BaseHTTPRequestHandler):
    server_version = "LicitaBot/0.1"
    db_path: Path | None = None
    permitir_todas = False

    # ------------------------------------------------------------- utilidades
    def _autorizado(self) -> bool:
        if self.permitir_todas:
            return True
        ip = self.client_address[0] if self.client_address else ""
        return any(ip.startswith(p) for p in FAIXAS_PERMITIDAS)

    def _json(self, dados, status: int = 200) -> None:
        corpo = json.dumps(dados, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corpo)

    def _erro(self, msg: str, status: int = 400) -> None:
        self._json({"erro": msg}, status)

    def _arquivo(self, caminho: Path) -> None:
        if not caminho.exists() or not caminho.is_file():
            self._erro("não encontrado", 404); return
        # impede path traversal: o caminho resolvido tem de estar sob web/
        try:
            caminho.resolve().relative_to(WEB_DIR.resolve())
        except ValueError:
            self._erro("acesso negado", 403); return
        corpo = caminho.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         TIPOS.get(caminho.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, fmt, *args):
        # log enxuto: só o essencial, sem poluir o journal
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ------------------------------------------------------------------ rotas
    def do_GET(self) -> None:  # noqa: N802 (API do http.server)
        if not self._autorizado():
            self._erro(
                "acesso restrito. A interface libera apenas localhost e o "
                "tailnet. Use --host 0.0.0.0 --permitir-todas para expor, "
                "de preferência atrás de autenticação.", 403)
            return

        parsed = urllib.parse.urlparse(self.path)
        rota = parsed.path.rstrip("/") or "/"
        q = urllib.parse.parse_qs(parsed.query)

        try:
            conn = abrir_ro(self.db_path)
        except FileNotFoundError as e:
            self._erro(str(e), 503); return

        try:
            if rota in ("/", "/index.html"):
                return self._arquivo(WEB_DIR / "index.html")
            if rota == "/resultados":
                return self._arquivo(WEB_DIR / "resultados.html")
            if rota.startswith("/static/"):
                # mantém o subdiretório: /static/estilo.css -> web/static/estilo.css
                return self._arquivo(WEB_DIR / rota.lstrip("/"))

            if rota == "/api/licitacoes":
                return self._json(consultar_paginado(
                    conn, "oportunidades", q,
                    pagina=_int(q.get("pagina"), 1),
                    por_pagina=_int(q.get("por_pagina"), 25),
                    ordem=_str(q.get("ordem")),
                ))

            if rota == "/api/contratos":
                return self._json(consultar_paginado(
                    conn, "contratos", q,
                    pagina=_int(q.get("pagina"), 1),
                    por_pagina=_int(q.get("por_pagina"), 25),
                    ordem=_str(q.get("ordem")),
                ))

            if rota == "/api/facetas":
                tabela = "contratos" if _str(q.get("tabela")) == "contratos" else "oportunidades"
                return self._json(facetas(conn, tabela, q))

            if rota == "/api/estatisticas":
                return self._json(estatisticas_gerais(conn))

            if rota == "/api/cobertura":
                from .web_queries import cobertura_campos
                return self._json(cobertura_campos(conn))

            if rota == "/api/exportar.csv":
                return self._exportar_csv(conn, q)

            self._erro("rota não encontrada", 404)
        except Exception as e:                      # nunca derruba o servidor
            self._erro(f"erro interno: {e}", 500)
        finally:
            conn.close()

    def _exportar_csv(self, conn, q) -> None:
        """Exporta o resultado filtrado em CSV."""
        import csv
        import io
        r = consultar_paginado(conn, "oportunidades", q, pagina=1,
                               por_pagina=200, ordem=_str(q.get("ordem")))
        itens = r["itens"]
        buf = io.StringIO()
        if itens:
            w = csv.DictWriter(buf, fieldnames=list(itens[0].keys()),
                               delimiter=";", extrasaction="ignore")
            w.writeheader()
            w.writerows(itens)
        corpo = buf.getvalue().encode("utf-8-sig")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition",
                         'attachment; filename="licitacoes.csv"')
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)


def _str(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _int(v, default):
    try:
        return int(_str(v))
    except (TypeError, ValueError):
        return default


def rodar(host: str = "127.0.0.1", porta: int = 8080,
          db_path: Path | str | None = None,
          permitir_todas: bool = False) -> None:
    Handler.db_path = Path(db_path) if db_path else DB_PATH
    Handler.permitir_todas = permitir_todas
    # confere a base antes de subir, para falhar com mensagem clara
    abrir_ro(Handler.db_path).close()

    srv = ThreadingHTTPServer((host, porta), Handler)
    url = f"http://{host if host != '0.0.0.0' else 'localhost'}:{porta}"
    print(f"LicitaBot — interface no ar")
    print(f"  licitações : {url}/")
    print(f"  resultados : {url}/resultados")
    print(f"  API        : {url}/api/licitacoes?uf=SP&abertos=1")
    print(f"  base       : {Handler.db_path}")
    if host == "0.0.0.0":
        if permitir_todas:
            print("  ATENÇÃO: aberto para qualquer origem (--permitir-todas).")
        else:
            print("  acesso liberado apenas para localhost e o tailnet (100.x).")
    print("  (Ctrl+C para parar)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nencerrando…")
    finally:
        srv.server_close()


if __name__ == "__main__":
    rodar()
