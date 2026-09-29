/* LicitaBot — utilitários compartilhados pelas duas interfaces.
   Sem framework: funções pequenas, explícitas, sem etapa de build. */

// ------------------------------------------------------------------ formato
const fmtMoeda = (v) => {
  if (v === null || v === undefined || v === '') return '—';
  const n = Number(v);
  if (!isFinite(n) || n === 0) return '—';
  if (n >= 1e9) return 'R$ ' + (n / 1e9).toFixed(2).replace('.', ',') + ' bi';
  if (n >= 1e6) return 'R$ ' + (n / 1e6).toFixed(2).replace('.', ',') + ' mi';
  if (n >= 1e3) return 'R$ ' + (n / 1e3).toFixed(0) + ' mil';
  return 'R$ ' + n.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};

const fmtMoedaCheia = (v) => {
  if (!v) return '—';
  return 'R$ ' + Number(v).toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};

const fmtNum = (v) => (v === null || v === undefined) ? '—' : Number(v).toLocaleString('pt-BR');

const fmtData = (s) => {
  if (!s) return '—';
  const d = String(s).slice(0, 10).split('-');
  return d.length === 3 ? `${d[2]}/${d[1]}/${d[0]}` : s;
};

// O PNCP devolve datas em horário de Brasília (UTC-3), sem fuso no campo.
// Comparar com UTC erra por 3h e faz uma licitação que vence hoje parecer
// vencida — foi um bug real que escondia as oportunidades mais urgentes.
const DIAS = (s) => {
  if (!s) return null;
  const d = new Date(String(s).replace(' ', 'T') + '-03:00');
  if (isNaN(d)) return null;
  return (d - new Date()) / 86400000;
};

const classePrazo = (s) => {
  const d = DIAS(s);
  if (d === null) return ['', ''];
  if (d < 0)                       return ['prazo-venc', 'encerrada'];
  if (d < 1)                       return ['prazo-hoje', 'HOJE'];
  if (d < 3)                       return ['prazo-prox', Math.floor(d) + 'd'];
  return ['prazo-ok', Math.floor(d) + 'd'];
};

const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// Rótulos legíveis para valores de código
const ROTULOS = {
  F: 'Federal', E: 'Estadual', M: 'Municipal',
  S: 'Serviço', M_: 'Material',
  alta: 'alta', media: 'média', baixa: 'baixa',
};
const rotulo = (campo, v) => {
  if (v === null || v === undefined || v === '') return '—';
  if (campo === 'esfera') return ROTULOS[v] || v;
  if (campo === 'material_ou_servico') return v === 'S' ? 'Serviço' : 'Material';
  return v;
};

// ------------------------------------------------------------------- estado
/* O estado dos filtros vive na URL (?uf=SP&setor=tecnologia).
   Isso dá link compartilhável, botão voltar funcionando e recarga que não
   perde o filtro — três coisas que um estado só em memória não dá. */
const Parametros = {
  ler() {
    const p = new URLSearchParams(location.search);
    const o = {};
    for (const [k, v] of p.entries()) {
      if (v === '') continue;
      if (k.endsWith('[]')) {
        const key = k.slice(0, -2);
        (o[key] = o[key] || []).push(v);
      } else o[k] = v;
    }
    return o;
  },
  escrever(obj) {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(obj)) {
      if (v === null || v === undefined || v === '' || v === false) continue;
      if (v === true) { p.append(k, '1'); continue; }
      if (Array.isArray(v)) v.forEach((x) => p.append(k + '[]', x));
      else p.set(k, v);
    }
    const s = p.toString();
    history.replaceState(null, '', s ? '?' + s : location.pathname);
  },
};

// ------------------------------------------------------------------ fetch
async function api(rota, params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v === null || v === undefined || v === '' || v === false) continue;
    if (v === true) { p.append(k, '1'); continue; }
    if (Array.isArray(v)) v.forEach((x) => p.append(k + '[]', x));
    else p.set(k, v);
  }
  const r = await fetch(rota + (p.toString() ? '?' + p : ''));
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).erro || r.statusText);
  return r.json();
}

// Renderiza a lista de checkboxes de uma faceta
function facetasHTML(campo, facetas, selecionados, onChange) {
  const dados = facetas[campo];
  if (!dados || !dados.valores.length) return '<div class="vazio">sem valores</div>';
  const sel = Array.isArray(selecionados) ? selecionados : (selecionados ? [selecionados] : []);
  return dados.valores.map((v) => `
    <label>
      <input type="checkbox" value="${esc(v.valor)}" ${sel.includes(String(v.valor)) ? 'checked' : ''}>
      <span>${esc(rotulo(campo, v.valor))}</span>
      <span class="n">${fmtNum(v.n)}</span>
    </label>`).join('');
}

function paginacaoHTML(pag) {
  if (pag.paginas <= 1) return '';
  return `<div class="paginacao">
    <button ${pag.pagina <= 1 ? 'disabled' : ''} data-pag="${pag.pagina - 1}">← anterior</button>
    <span class="pag">página ${pag.pagina} de ${fmtNum(pag.paginas)}</span>
    <button ${pag.pagina >= pag.paginas ? 'disabled' : ''} data-pag="${pag.pagina + 1}">próxima →</button>
  </div>`;
}
