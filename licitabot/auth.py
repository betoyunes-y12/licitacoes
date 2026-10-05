"""Autenticação HTTP Basic para a interface web.

POR QUE ISTO EXISTE
-------------------
A interface nasceu protegida só por estar no tailnet. Isso funciona enquanto
todo acesso vem de dentro da rede privada, mas quebra em dois cenários reais:

  1. expor por `tailscale funnel` (que publica na internet);
  2. permitir acesso de alguém fora do tailnet (cliente, sócio, contador).

Sem autenticação, qualquer um que alcance a URL lê a base inteira — que tem
dado comercial e a estratégia de prospecção.

POR QUE HTTP BASIC E NÃO ALGO MELHOR
------------------------------------
Basic é fraco isoladamente (credencial em base64 a cada request), mas:
  • sempre trafega sob HTTPS, que é o caso aqui (Tailscale fornece TLS);
  • tem suporte nativo em qualquer navegador, sem tela de login para manter;
  • é o mesmo padrão já usado no servidor (nginx com .htpasswd do Viminas).

Para um painel interno de leitura, é proporcional. Se virar produto com vários
usuários, trocar por sessão assinada com senha em hash forte (bcrypt/argon2).

FORMATO DO ARQUIVO
------------------
Reaproveita o formato `.htpasswd` do Apache/nginx, para o usuário poder gerar
com `htpasswd -B` se preferir:
    usuario:hash
Suportado: bcrypt (`$2y$`/`$2b$`) e SHA1 (`{SHA}`).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from pathlib import Path

ARQ_PADRAO = Path(__file__).resolve().parent / "data" / ".htpasswd"


def gerar_hash(senha: str) -> str:
    """Gera hash SHA1 no formato `{SHA}` (compatível com htpasswd)."""
    d = hashlib.sha1(senha.encode("utf-8")).digest()
    return "{SHA}" + base64.b64encode(d).decode("ascii")


def gerar_senha(tamanho: int = 20) -> str:
    """Senha aleatória legível: sem caracteres ambíguos (0/O, 1/l/I).

    Serve para o usuário copiar e colar sem erro de digitação.
    """
    alfabeto = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alfabeto) for _ in range(tamanho))


def criar_arquivo(caminho: Path | None = None, usuario: str = "licitabot",
                  senha: str | None = None) -> tuple[Path, str, str]:
    """Cria o .htpasswd. Devolve (caminho, usuario, senha)."""
    caminho = Path(caminho) if caminho else ARQ_PADRAO
    senha = senha or gerar_senha()
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(f"{usuario}:{gerar_hash(senha)}\n", encoding="utf-8")
    caminho.chmod(0o600)
    return caminho, usuario, senha


def _carregar(caminho: Path) -> dict[str, str]:
    if not caminho.exists():
        return {}
    saida: dict[str, str] = {}
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or ":" not in linha:
            continue
        u, _, h = linha.partition(":")
        saida[u.strip()] = h.strip()
    return saida


def _confere(senha: str, hash_armazenado: str) -> bool:
    """Compara a senha com o hash. Suporta `{SHA}` e bcrypt."""
    if hash_armazenado.startswith("{SHA}"):
        return hmac.compare_digest(gerar_hash(senha), hash_armazenado)
    if hash_armazenado.startswith(("$2y$", "$2b$", "$2a$")):
        try:
            import bcrypt  # opcional
            return bcrypt.checkpw(senha.encode(), hash_armazenado.encode())
        except ImportError:
            return False
    return False


def verificar(cabecalho: str | None, caminho: Path | None = None) -> str | None:
    """Valida o header `Authorization: Basic ...`.

    Devolve o nome do usuário autenticado, ou None. Usa `hmac.compare_digest`
    na comparação final para não vazar informação por tempo de resposta.
    """
    if not cabecalho or not cabecalho.lower().startswith("basic "):
        return None
    try:
        bruto = base64.b64decode(cabecalho.split(" ", 1)[1]).decode("utf-8")
        usuario, _, senha = bruto.partition(":")
    except Exception:
        return None

    usuarios = _carregar(Path(caminho) if caminho else ARQ_PADRAO)
    h = usuarios.get(usuario)
    if h and _confere(senha, h):
        return usuario
    return None


def configurado(caminho: Path | None = None) -> bool:
    """Diz se há usuário cadastrado. Sem arquivo, a interface fica sem auth."""
    return bool(_carregar(Path(caminho) if caminho else ARQ_PADRAO))
