"""Coletor do PNCP — Portal Nacional de Contratações Públicas.

Fonte PRIMÁRIA do agente. Três razões técnicas:
  1. É obrigatório por lei (Lei 14.133/2021, art. 174) e agrega União, Estados
     e Municípios num único endpoint — cobre ~5.570 municípios sem ter que
     integrar 5.570 portais.
  2. A busca textual (/api/search/) aceita `q` e `status=recebendo_proposta`,
     permitindo filtrar por palavras-chave e por prazo em aberto.
  3. É API pública, documentada e estável — sem scraping de HTML.

Endpoints verificados ao vivo (2026-09-29):
  GET /api/search/?q=&tipos_documento=edital&status=recebendo_proposta
  GET /api/consulta/v1/contratacoes/proposta?dataFinal=&codigoModalidadeContratacao=
  GET /api/consulta/v1/contratacoes/publicacao?dataInicial=&dataFinal=&codigoModalidadeContratacao=
  GET /api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}/itens
"""
from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from ..http import UA, Client, HttpError
from ..store import now_iso

BASE = "https://pncp.gov.br/api"

# O /api/search exige estes parâmetros; sem eles devolve 200 com corpo vazio.
HEADERS_BUSCA = {"Referer": "https://pncp.gov.br/app/editais"}

MODALIDADES_VALIDAS = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13}


def _parse_dt(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class PNCPCollector:
    fonte = "pncp"

    def __init__(self, client: Client | None = None, **kw):
        self.client = client or Client(**kw)
        # O /api/search exige Referer, mas NÃO pode ter throttle independente:
        # dois clientes com delay próprio dobram a taxa efetiva de requisições
        # e foi assim que o limite do portal foi atingido nos testes.
        # Compartilhamos o mesmo portão de tempo entre os dois.
        self.search_client = Client(
            extra_headers=HEADERS_BUSCA, on_rate_limit=kw.get("on_rate_limit"), **kw
        )
        self.search_client._gate = self.client  # mesmo relógio de throttle

    # ------------------------------------------------------- busca textual
    def buscar_editais(
        self,
        termo: str,
        *,
        uf: str | None = None,
        status: str = "recebendo_proposta",
        tipos_documento: str = "edital",
        pagina: int = 1,
        tamanho_pagina: int = 20,
        todos_status: bool = False,
    ) -> dict:
        """Busca textual de editais. `status=recebendo_proposta` = prazo aberto."""
        params = {
            "q": termo,
            "tipos_documento": tipos_documento,
            "pagina": pagina,
            "tamanhoPagina": min(tamanho_pagina, 50),
            "ordenacao": "-data",
        }
        if not todos_status:
            params["status"] = status
        if uf:
            params["ufs"] = uf
        try:
            return self.search_client.get_json(f"{BASE}/search/", params) or {}
        except HttpError as e:
            if e.status == 422:
                return {"items": [], "total": 0}
            raise

    def normalizar_busca(self, item: dict) -> dict:
        """Converte um resultado do /api/search no schema interno."""
        url_path = item.get("item_url") or ""
        link = f"https://pncp.gov.br/app{url_path}" if url_path.startswith("/") else url_path
        # item_url = /compras/{cnpj}/{ano}/{sequencial}
        cnpj = ano = seq = None
        m = re.match(r"^/compras/(\d{14})/(\d{4})/(\d+)$", url_path)
        if m:
            cnpj, ano, seq = m.group(1), int(m.group(2)), int(m.group(3))
        return {
            "id": item.get("id") or url_path or item.get("title"),
            "fonte": self.fonte,
            "orgao": item.get("orgao_nome") or item.get("title"),
            "cnpj_orgao": cnpj,
            "uf": item.get("uf"),
            "municipio": item.get("municipio_nome"),
            "esfera": item.get("esfera_nome"),
            "modalidade": item.get("modalidade_licitacao_nome") or item.get("tipo_documento"),
            "numero_compra": None,
            "ano": ano,
            "sequencial": seq,
            "objeto": (item.get("description") or "").strip(),
            "valor_estimado": item.get("valor_total_estimado"),
            "data_publicacao": item.get("data_publicacao"),
            "data_abertura": item.get("data_abertura_proposta"),
            "data_encerramento": item.get("data_encerramento_proposta"),
            "link": link,
            "situacao": item.get("status_nome") or item.get("status"),
            "raw_json": item,
            "coletado_em": now_iso(),
        }

    def coletar_por_termos(
        self,
        termos: list[str],
        *,
        ufs: list[str] | None = None,
        max_paginas: int = 3,
        somente_abertos: bool = True,
        tamanho_pagina: int = 20,
        callback=None,
    ) -> list[dict]:
        """Varre N termos × M UFs. Deduplica por id."""
        vistos: dict[str, dict] = {}
        ufs_iter = ufs or [None]
        for termo in termos:
            for uf in ufs_iter:
                for pagina in range(1, max_paginas + 1):
                    try:
                        resp = self.buscar_editais(
                            termo, uf=uf, pagina=pagina,
                            tamanho_pagina=tamanho_pagina,
                            todos_status=not somente_abertos,
                        )
                    except Exception as e:  # não aborta a varredura inteira
                        if callback:
                            callback("erro", f"{termo}/{uf} p{pagina}: {e}")
                        break
                    itens = resp.get("items") or []
                    if not itens:
                        break
                    for it in itens:
                        op = self.normalizar_busca(it)
                        if op["id"] and op["id"] not in vistos:
                            vistos[op["id"]] = op
                    if callback:
                        callback("pagina", f"{termo}/{uf} p{pagina}: +{len(itens)} (total {len(vistos)})")
                    if len(itens) < tamanho_pagina:
                        break
        return list(vistos.values())

    # ------------------------------------------- oportunidades por prazo
    def coletar_por_prazo(
        self,
        *,
        dias: int = 7,
        modalidades: tuple[int, ...] = (6, 5, 7),
        max_paginas: int = 10,
        callback=None,
    ) -> list[dict]:
        """/contratacoes/proposta devolve tudo com prazo encerrando na janela.

        Verificado ao vivo: janela de 8 dias na modalidade 6 -> 9.831 registros.
        """
        fim = (datetime.now() + timedelta(days=dias)).strftime("%Y%m%d")
        out: dict[str, dict] = {}
        for mod in modalidades:
            if mod not in MODALIDADES_VALIDAS:
                continue
            for pagina in range(1, max_paginas + 1):
                try:
                    resp = self.client.get_json(
                        f"{BASE}/consulta/v1/contratacoes/proposta",
                        {"dataFinal": fim, "codigoModalidadeContratacao": mod, "pagina": pagina},
                    )
                except Exception as e:
                    if callback:
                        callback("erro", f"modalidade {mod} p{pagina}: {e}")
                    break
                if not resp:
                    break
                dados = resp.get("data") or []
                if not dados:
                    break
                for it in dados:
                    op = self.normalizar_consulta(it)
                    if op["id"]:
                        out[op["id"]] = op
                if callback:
                    callback(
                        "pagina",
                        f"mod {mod} p{pagina}: +{len(dados)} "
                        f"(total {len(out)}/{resp.get('totalRegistros')})",
                    )
                if len(dados) < 50:
                    break
        return list(out.values())

    def normalizar_consulta(self, item: dict) -> dict:
        """Extrai TODOS os campos informativos de /contratacoes/proposta|publicacao.

        São 38 campos no payload (verificado no exemplo
        18428839000190-1-000160/2026). Guardamos os principais como colunas
        próprias — para filtro e índice — e o payload inteiro em raw_json.
        """
        orgao = item.get("orgaoEntidade") or {}
        unidade = item.get("unidadeOrgao") or {}
        amparo = item.get("amparoLegal") or {}
        sub_orgao = item.get("orgaoSubRogado") or {}
        sub_unidade = item.get("unidadeSubRogada") or {}
        ano = item.get("anoCompra")
        seq = item.get("sequencialCompra")
        cnpj = orgao.get("cnpj")
        nctrl = item.get("numeroControlePNCP") or (
            f"{cnpj}-1-{seq:06d}/{ano}" if cnpj and seq and ano else None
        )
        link = (
            f"https://pncp.gov.br/app/editais/{cnpj}/{ano}/{seq}"
            if cnpj and ano and seq else "https://pncp.gov.br/app/editais"
        )
        return {
            "id": nctrl,
            "fonte": self.fonte,

            # órgão e localização
            "orgao": orgao.get("razaoSocial"),
            "cnpj_orgao": cnpj,
            "uf": unidade.get("ufSigla"),
            "municipio": unidade.get("municipioNome"),
            "codigo_ibge": unidade.get("codigoIbge"),
            "esfera": orgao.get("esferaId"),
            "poder": orgao.get("poderId"),
            "unidade_nome": unidade.get("nomeUnidade"),
            "unidade_codigo": unidade.get("codigoUnidade"),
            "orgao_subrogado": sub_orgao.get("razaoSocial") if sub_orgao else None,
            "unidade_subrogada": sub_unidade.get("nomeUnidade") if sub_unidade else None,

            # identificação
            "modalidade": item.get("modalidadeNome"),
            "modalidade_id": item.get("modalidadeId"),
            "modo_disputa": item.get("modoDisputaNome"),
            "instrumento": item.get("tipoInstrumentoConvocatorioNome"),
            "numero_compra": item.get("numeroCompra"),
            "processo": item.get("processo"),
            "ano": ano,
            "sequencial": seq,

            # objeto e valores
            "objeto": (item.get("objetoCompra") or "").strip(),
            "informacao_complementar": item.get("informacaoComplementar"),
            "valor_estimado": item.get("valorTotalEstimado"),
            "valor_homologado": item.get("valorTotalHomologado"),
            "srp": item.get("srp"),
            "orcamento_sigiloso": item.get("orcamentoSigilosoCodigo"),
            "emenda_parlamentar": item.get("emendaParlamentar"),

            # base legal
            "amparo_legal": amparo.get("nome"),
            "amparo_legal_codigo": amparo.get("codigo"),
            "amparo_legal_descricao": amparo.get("descricao"),

            # datas
            "data_publicacao": item.get("dataPublicacaoPncp") or item.get("dataInclusao"),
            "data_abertura": item.get("dataAberturaProposta"),
            "data_encerramento": item.get("dataEncerramentoProposta"),
            "data_inclusao": item.get("dataInclusao"),
            "data_atualizacao": item.get("dataAtualizacao") or item.get("dataAtualizacaoGlobal"),

            # situação
            "situacao": item.get("situacaoCompraNome") or item.get("situacaoCompra"),
            "situacao_id": item.get("situacaoCompraId"),
            "existe_resultado": item.get("existeResultado"),

            # links
            "link": link,
            "link_sistema_origem": item.get("linkSistemaOrigem"),
            "link_processo": item.get("linkProcessoEletronico"),

            # controle
            "usuario_nome": item.get("usuarioNome"),
            "raw_json": item,
            "coletado_em": now_iso(),
        }

    # ------------------------------------------------------------- itens
    def coletar_itens(self, cnpj: str, ano: int, sequencial: int) -> list[dict]:
        """Itens da contratação. Endpoint verificado ao vivo.

        ATENÇÃO aos nomes reais dos campos (diferem do que se esperaria):
        idCompraItem, descricaoResumida, descricaodetalhada, itemCategoriaNome.
        """
        dados = self.client.get_json(
            f"{BASE}/pncp/v1/orgaos/{cnpj}/compras/{ano}/{sequencial}/itens"
        )
        if not dados:
            return []
        out = []
        for it in dados:
            out.append({
                "id_item": it.get("idCompraItem")
                or f"{cnpj}-{ano}-{sequencial}-{it.get('numeroItem')}",
                "id_compra": it.get("idContratacaoPNCP")
                or f"{cnpj}-1-{sequencial:06d}/{ano}",
                "numero_item": it.get("numeroItem"),
                # O campo é `descricao` (verificado), não descricaoResumida —
                # este último é o nome usado no endpoint de ITENS da
                # contratação (modulo-contratacoes do Compras.gov.br).
                # Aceitamos os dois para não depender de qual endpoint veio.
                "descricao": (it.get("descricao") or it.get("descricaoResumida")
                              or it.get("descricaodetalhada")),
                "descricao_detalhada": (it.get("descricaodetalhada")
                                        or it.get("descricao")
                                        or it.get("descricaoResumida") or ""),
                "material_ou_servico": it.get("materialOuServico"),
                "material_ou_servico_nome": it.get("materialOuServicoNome"),
                "categoria": it.get("itemCategoriaNome"),
                "categoria_id": it.get("itemCategoriaId"),
                "quantidade": it.get("quantidade"),
                "unidade_medida": it.get("unidadeMedida"),
                "valor_unitario": it.get("valorUnitarioEstimado"),
                "valor_total": it.get("valorTotal"),
                "criterio_julgamento": it.get("criterioJulgamentoNome"),
                "criterio_julgamento_id": it.get("criterioJulgamentoId"),
                "situacao": it.get("situacaoCompraItemNome"),
                "situacao_id": it.get("situacaoCompraItem"),
                "tipo_beneficio": it.get("tipoBeneficioNome"),
                "tipo_beneficio_id": it.get("tipoBeneficio"),
                "incentivo_produtivo_basico": it.get("incentivoProdutivoBasico"),
                "margem_normal": it.get("aplicabilidadeMargemPreferenciaNormal"),
                "margem_adicional": it.get("aplicabilidadeMargemPreferenciaAdicional"),
                "percentual_margem_normal": it.get("percentualMargemPreferenciaNormal"),
                "percentual_margem_adicional": it.get("percentualMargemPreferenciaAdicional"),
                "tipo_margem_preferencia": it.get("tipoMargemPreferencia"),
                "conteudo_nacional": it.get("exigenciaConteudoNacional"),
                "orcamento_sigiloso": it.get("orcamentoSigiloso"),
                "tem_resultado": it.get("temResultado"),
                "ncm_nbs_codigo": it.get("ncmNbsCodigo"),
                "ncm_nbs_descricao": it.get("ncmNbsDescricao"),
                "catalogo": it.get("catalogo"),
                "catalogo_codigo_item": it.get("catalogoCodigoItem"),
                "categoria_catalogo": it.get("categoriaItemCatalogo"),
                "patrimonio": it.get("patrimonio"),
                "codigo_registro_imobiliario": it.get("codigoRegistroImobiliario"),
                "informacao_complementar": it.get("informacaoComplementar"),
                "data_inclusao": it.get("dataInclusao"),
                "data_atualizacao": it.get("dataAtualizacao"),
            })
        return out

    def detectar_tipo(self, op: dict) -> dict:
        """Extrai dos itens os campos que só existem nesse nível.

        São 36 campos por item (verificado). Os mais valiosos para filtro:

          tipoBeneficioNome            — ME/EPP, agricultura familiar…
                                         benefício da LC 123 é vantagem competitiva real
          aplicabilidadeMargemPreferenciaNormal/Adicional — margem de preferência
          exigenciaConteudoNacional    — exige conteúdo local
          criterioJulgamentoNome       — menor preço, técnica e preço…
          situacaoCompraItemNome       — em andamento, homologado, fracassado
          ncmNbsCodigo / catalogo      — classificação para cruzar com catálogo

        Também define material-vs-serviço e monta `itens_texto` (descrições),
        para o matcher enxergar além do objeto resumido.
        """
        cnpj, ano, seq = op.get("cnpj_orgao"), op.get("ano"), op.get("sequencial")
        if not (cnpj and ano and seq):
            return op
        try:
            itens = self.coletar_itens(cnpj, ano, seq)
        except Exception:
            return op
        if not itens:
            return op

        tipos = [i.get("material_ou_servico") for i in itens if i.get("material_ou_servico")]
        if tipos:
            n_s = tipos.count("S")
            op["material_ou_servico"] = "S" if n_s >= len(tipos) / 2 else "M"

        cats = [i.get("categoria") for i in itens
                if i.get("categoria") and i["categoria"] != "Não se aplica"]
        if cats:
            op["categoria_item"] = sorted(set(cats))[0]

        # benefício ME/EPP: basta um item com benefício aplicável
        beneficios = {i.get("tipo_beneficio") for i in itens if i.get("tipo_beneficio")}
        beneficios.discard("Não se aplica")
        if beneficios:
            op["tipo_beneficio"] = " | ".join(sorted(beneficios))

        # margem de preferência e conteúdo nacional
        if any(i.get("margem_normal") for i in itens):
            op["margem_preferencia_normal"] = True
        if any(i.get("margem_adicional") for i in itens):
            op["margem_preferencia_adicional"] = True
        if any(i.get("conteudo_nacional") for i in itens):
            op["conteudo_nacional"] = True

        descricoes = " ".join(filter(None, (i.get("descricao") or "" for i in itens)))
        if descricoes:
            op["itens_texto"] = descricoes[:4000]

        op["qtd_itens"] = len(itens)
        op["itens_json"] = json.dumps(itens, ensure_ascii=False)
        return op

    def detalhe(self, cnpj: str, ano: int, seq: int) -> dict | None:
        return self.client.get_json(f"{BASE}/consulta/v1/orgaos/{cnpj}/compras/{ano}/{seq}")

    # --------------------------------------------------------- documentos
    def listar_documentos(self, cnpj: str, ano: int, seq: int) -> list[dict]:
        """Arquivos anexados à contratação (edital, termo de referência, minuta).

        Endpoint verificado ao vivo — devolve título, tipo e URL de download:
            GET /api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}/arquivos
        O download do binário é feito em `baixar_documento`.
        """
        dados = self.client.get_json(
            f"{BASE}/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}/arquivos"
        )
        if not isinstance(dados, list):
            return []
        return [
            {
                "titulo": d.get("titulo"),
                "tipo": d.get("tipoDocumentoNome"),
                "url": d.get("url") or d.get("uri"),
                "sequencial": d.get("sequencialDocumento"),
                "publicado_em": d.get("dataPublicacaoPncp"),
                "ativo": d.get("statusAtivo"),
            }
            for d in dados
        ]

    def baixar_documento(self, url: str, destino: str | Path, *,
                         timeout: int = 90, sobrescrever: bool = False) -> Path | None:
        """Baixa um arquivo (PDF etc.) para o disco. Devolve o caminho ou None.

        Não usa o cache JSON — é binário. Valida que veio conteúdo antes de
        gravar, para não deixar PDF corrompido de 0 byte no disco.
        """
        destino = Path(destino)
        if destino.exists() and not sobrescrever and destino.stat().st_size > 0:
            return destino
        destino.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                blob = resp.read()
            if not blob:
                return None
            destino.write_bytes(blob)
            return destino
        except Exception:
            return None

    # ---------------------------------------------------------- enriquecer
    def enriquecer(self, op: dict) -> dict:
        """Preenche prazo/valor/modalidade/tipo a partir do detalhe da contratação.

        Necessário porque o /api/search (busca textual) NÃO devolve
        dataEncerramentoProposta nem valorTotalEstimado. Sem isso, o agente
        não consegue alertar por prazo — que é justamente o dado crítico.
        Verificado ao vivo: o detalhe devolve ambos os campos.
        """
        cnpj, ano, seq = op.get("cnpj_orgao"), op.get("ano"), op.get("sequencial")
        if not (cnpj and ano and seq):
            return op
        try:
            det = self.detalhe(cnpj, ano, seq)
        except Exception:
            det = None
        if det:
            # campos que só existem no /detalhe (a listagem não os traz)
            for campo, chave in (
                ("data_encerramento", "dataEncerramentoProposta"),
                ("data_abertura", "dataAberturaProposta"),
                ("valor_estimado", "valorTotalEstimado"),
                ("valor_homologado", "valorTotalHomologado"),
                ("numero_compra", "numeroCompra"),
                ("processo", "processo"),
                ("modo_disputa", "modoDisputaNome"),
                ("instrumento", "tipoInstrumentoConvocatorioNome"),
                ("informacao_complementar", "informacaoComplementar"),
                ("situacao", "situacaoCompraNome"),
                ("situacao_id", "situacaoCompraId"),
                ("existe_resultado", "existeResultado"),
                ("srp", "srp"),
                ("orcamento_sigiloso", "orcamentoSigilosoCodigo"),
                ("emenda_parlamentar", "emendaParlamentar"),
                ("data_publicacao", "dataPublicacaoPncp"),
                ("data_inclusao", "dataInclusao"),
                ("data_atualizacao", "dataAtualizacao"),
                ("link_sistema_origem", "linkSistemaOrigem"),
                ("link_processo", "linkProcessoEletronico"),
                ("usuario_nome", "usuarioNome"),
                ("modalidade", "modalidadeNome"),
                ("modalidade_id", "modalidadeId"),
            ):
                if op.get(campo) in (None, "") and det.get(chave) is not None:
                    op[campo] = det[chave]
            amparo = det.get("amparoLegal") or {}
            if amparo:
                op["amparo_legal"] = amparo.get("nome")
                op["amparo_legal_codigo"] = amparo.get("codigo")
                op["amparo_legal_descricao"] = amparo.get("descricao")
            op["detalhe_json"] = json.dumps(det, ensure_ascii=False)

        # itens: definem material vs serviço, benefício ME/EPP e categoria
        self.detectar_tipo(op)
        op["_enriquecido"] = True
        return op

    def enriquecer_lote(self, ops: list[dict], *, limite: int | None = None,
                        callback=None) -> list[dict]:
        alvo = ops[:limite] if limite else ops
        for i, op in enumerate(alvo, 1):
            self.enriquecer(op)
            if callback and i % 10 == 0:
                callback("progresso", f"enriquecidos {i}/{len(alvo)}")
        return ops
