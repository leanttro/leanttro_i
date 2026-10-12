"""
MCMV — Minha Casa Minha Vida (módulo separado do app.py).

COMO LIGAR no app.py (cole no fim do arquivo, logo ANTES de `if DATABASE_URL: _bootstrap()`):

    try:
        import mcmv
        mcmv.init(app)
    except Exception:
        log.exception("MCMV: o módulo não carregou (o resto do site segue normal)")

Coloque o mcmv.py na mesma pasta do app.py (se o seu Docker copia só o app.py, inclua este arquivo também).

O que ele cria (sem banco, sem tabela nova, sem mexer em nada que já existe):
    GET /minha-casa-minha-vida                 página com o simulador, faixas e perguntas frequentes
    GET /api/mcmv/simular?renda=4500&entrada=0 JSON do simulador (a página e, no futuro, a Duda usam a mesma conta)
    link para o menu: {% if tem_rota('mcmv.pagina') %}<a href="{{ url_for('mcmv.pagina') }}">Minha Casa Minha Vida</a>{% endif %}

QUANDO A REGRA DO PROGRAMA MUDAR: edite só o bloco REGRAS logo abaixo (e ATUALIZADO_EM).
"""
import re

from flask import Blueprint, jsonify, redirect, render_template_string, request, url_for

# ═══════════════════════════════════════════════════════════════
# REGRAS DO PROGRAMA — único lugar a editar quando o governo mudar algo
# ═══════════════════════════════════════════════════════════════
ATUALIZADO_EM = "outubro/2026"
PRAZO_MESES = 420            # até 35 anos, em todas as faixas
COMPROMETIMENTO = 0.30       # a parcela deve caber em 30% da renda familiar
# renda_max: teto de renda familiar da faixa · teto_min/teto: valor máximo do imóvel (nas faixas 1 e 2 depende da cidade)
# subsidio: desconto máximo do governo na entrada (só faixas 1 e 2)
# juros: % ao ano. ATENÇÃO: valores de REFERÊNCIA (usei o topo de cada grupo, para a estimativa ficar conservadora).
#        Não consegui confirmar os juros de cada faixa nas fontes consultadas — confira na Caixa e ajuste aqui.
REGRAS = (
    dict(faixa=1, renda_max=3200, teto_min=210_000, teto=275_000, subsidio=55_000, juros=4.5),
    dict(faixa=2, renda_max=5000, teto_min=210_000, teto=275_000, subsidio=55_000, juros=7.0),
    dict(faixa=3, renda_max=9600, teto_min=400_000, teto=400_000, subsidio=0, juros=8.16),
    dict(faixa=4, renda_max=13000, teto_min=600_000, teto=600_000, subsidio=0, juros=10.5),
)

bp = Blueprint("mcmv", __name__)


# ═══════════════════════════════════════════════════════════════
# CONTA DO SIMULADOR (função pura: dá para chamar de qualquer lugar, ex.: chatbot)
# ═══════════════════════════════════════════════════════════════
def _num(v):
    """'4.500,50' | '4500.5' | '1.200' -> float (ou None)."""
    s = re.sub(r"[^\d,.]", "", str(v or ""))
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") > 1 or re.fullmatch(r"\d{1,3}\.\d{3}", s):
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def simular(renda, entrada=0.0):
    """Estimativa de até quanto de imóvel cabe. Devolve dict (sempre com 'no_programa')."""
    renda, entrada = max(0.0, float(renda or 0)), max(0.0, float(entrada or 0))
    r = next((x for x in REGRAS if renda <= x["renda_max"]), None)
    if r is None:
        return dict(no_programa=False, limite_renda=REGRAS[-1]["renda_max"])
    parcela_max = renda * COMPROMETIMENTO
    i = (1 + r["juros"] / 100) ** (1 / 12) - 1                 # taxa ao mês
    financiavel = parcela_max / (1 / PRAZO_MESES + i)          # SAC: 1ª parcela = amortização + juros (a mais alta)
    poder = financiavel + entrada + r["subsidio"]
    imovel_ate = min(r["teto"], poder)
    return dict(
        no_programa=True, faixa=r["faixa"], renda_max=r["renda_max"], juros=r["juros"], prazo_anos=PRAZO_MESES // 12,
        parcela_max=round(parcela_max), financiavel=round(financiavel), subsidio_max=r["subsidio"], entrada=round(entrada),
        teto=r["teto"], teto_min=r["teto_min"], imovel_ate=int(imovel_ate // 1000 * 1000),
        busca_preco=int(imovel_ate // 5000 * 5000), limitado_pelo_teto=poder >= r["teto"],
        depende_cidade=r["teto_min"] < r["teto"] and imovel_ate > r["teto_min"])


def _brl(v):
    return "R$ " + f"{float(v):,.0f}".replace(",", ".")


# ═══════════════════════════════════════════════════════════════
# CONTEÚDO (as mesmas perguntas alimentam a página e o JSON-LD do Google)
# ═══════════════════════════════════════════════════════════════
def _faq():
    topo = REGRAS[-1]["renda_max"]
    return [
        ("Quem pode participar do Minha Casa Minha Vida?",
         f"Famílias urbanas com renda de até {_brl(topo)} por mês, dentro de uma das quatro faixas. Em geral também é preciso não ter outro "
         "imóvel em seu nome. A Caixa confere as regras do seu caso na hora da análise."),
        ("Qual é o prazo do financiamento?", f"Até {PRAZO_MESES // 12} anos ({PRAZO_MESES} meses), em todas as faixas."),
        ("Posso usar o FGTS?", "Em geral, sim: o saldo do FGTS pode entrar como parte da entrada, dentro das regras da Caixa."),
        ("Como o simulador calcula?",
         f"Ele considera que a parcela deve caber em {int(COMPROMETIMENTO * 100)}% da renda, o prazo de {PRAZO_MESES // 12} anos, "
         "juros de referência da sua faixa e, nas faixas 1 e 2, o subsídio máximo. É uma estimativa: juros, subsídio e valor aprovado "
         "dependem da análise da Caixa ou do banco operador."),
        ("Como contratar?",
         "Simule a parcela, escolha um imóvel dentro do limite da sua faixa e procure a Caixa pelo app, pelo site ou em uma agência. "
         "O OndeImóveis é um portal de anúncios: a negociação é direta com o anunciante e o financiamento é feito com o banco."),
    ]


PAGINA = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
{% set seo_title = 'Minha Casa Minha Vida ' ~ ano ~ ': simulador, faixas e imóveis | ' ~ SITE.nome %}
{% include '_seo.html' %}
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap">
<style>
:root{--g:#39e44b;--gd:#08a92e;--ink:#071421;--muted:#526071;--line:#dfe6e9;--soft:#f5f8f6;--mint:#eef8f1}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink);background:#fff}
a{color:inherit;text-decoration:none}button,input{font:inherit}
.container{width:min(1110px,calc(100% - 48px));margin:auto}
.logo{display:inline-flex;align-items:center;font-size:28px;font-weight:800;letter-spacing:-1.5px}.logo img{display:block;width:auto;filter:brightness(0)}
header{position:relative;z-index:5;background:var(--g);color:var(--ink);box-shadow:0 2px 14px #0002}
header .accent{color:#fff}
header .mark{display:inline-grid;place-items:center;width:29px;height:29px;border:3px solid var(--ink);border-radius:7px;margin-right:7px}
.nav{height:88px;display:flex;align-items:center;gap:40px}
.menu{display:flex;gap:30px;margin-left:auto;font-size:14px;font-weight:600}
.actions{display:flex;gap:20px;margin-left:12px;font-size:14px}.actions a{white-space:nowrap}
.actions .anunciar{display:none;background:var(--ink);color:#fff}
.menu a:hover,.actions a:hover{color:#fff}
.burger{display:none;width:44px;height:44px;padding:0;border:0;background:transparent;border-radius:10px;flex-direction:column;align-items:center;justify-content:center;gap:5px;cursor:pointer}
.burger span{display:block;width:25px;height:2.5px;background:var(--ink);border-radius:2px;transition:transform .2s,opacity .2s}
.burger[aria-expanded=true] span:nth-child(1){transform:translateY(7.5px) rotate(45deg)}
.burger[aria-expanded=true] span:nth-child(2){opacity:0}
.burger[aria-expanded=true] span:nth-child(3){transform:translateY(-7.5px) rotate(-45deg)}
.mnav{display:none;position:absolute;top:100%;left:0;right:0;background:var(--ink);border-top:1px solid #ffffff1f;box-shadow:0 16px 30px #0007;padding:8px 0 14px}
.mnav.on{display:block}.mnav a{display:block;padding:15px 22px;font-size:16px;font-weight:650;color:#fff}
.mnav hr{border:0;border-top:1px solid #ffffff24;margin:8px 22px}
@media(max-width:1000px){.menu{gap:15px}}
@media(max-width:700px){.container{width:calc(100% - 28px)}.nav{height:76px;gap:10px}.nav .logo img{height:52px!important}.menu,.actions .near{display:none}.actions{margin-left:auto}.actions .anunciar{display:inline-block;font-weight:800;padding:9px 15px;border-radius:20px}.actions a:not(.anunciar){display:none}.burger{display:flex}}
@media(min-width:701px){.mnav{display:none!important}}

.hero{background:var(--ink);color:#fff;padding:52px 0 60px}
.hero-in{display:grid;grid-template-columns:1fr 1.05fr;gap:44px;align-items:start}
.hero h1{font-size:clamp(32px,4vw,50px);line-height:1.03;letter-spacing:-2px;margin:0 0 16px}
.hero p{color:#cfd9de;font-size:16px;line-height:1.6;max-width:470px;margin:0}
.facts{list-style:none;padding:0;margin:28px 0 0;display:grid;gap:12px;font-size:14px;color:#e3e9ee}
.facts li{padding-left:14px;border-left:3px solid var(--g);line-height:1.45}
.card{background:#fff;color:var(--ink);border-radius:16px;padding:26px;box-shadow:0 18px 50px #0005}
.card h2{font-size:21px;margin:0 0 18px;letter-spacing:-.5px}
.row{margin-bottom:16px}
label{display:block;font-size:13px;font-weight:700;margin:0 0 6px}
.hint{font-weight:500;color:var(--muted)}
.inp{display:flex;align-items:center;border:1.5px solid var(--line);border-radius:10px;padding:0 14px;height:50px;background:#fff}
.inp:focus-within{border-color:var(--g);outline:3px solid #39e44b33}
.inp span{color:var(--muted);font-weight:700;margin-right:8px}
.inp input{border:0;outline:0;width:100%;height:100%;font-size:17px;font-weight:700;background:none}
.btn{width:100%;height:52px;border:0;border-radius:10px;background:var(--g);color:#06130b;font-weight:850;font-size:15px;cursor:pointer}
.btn:focus-visible,.go:focus-visible{outline:3px solid var(--ink);outline-offset:2px}
.btn[disabled]{opacity:.6;cursor:wait}
.res{margin-top:18px}.res:empty{display:none}
.out{background:var(--mint);border-radius:12px;padding:18px}
.chip{display:inline-block;background:var(--ink);color:#fff;border-radius:20px;padding:4px 12px;font-size:12px;font-weight:800}
.big{font-size:34px;font-weight:850;letter-spacing:-1.2px;margin:10px 0 2px}
.small{font-size:13px;color:var(--muted);line-height:1.5;margin:0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:14px 0}
.grid div{background:#fff;border-radius:10px;padding:10px 12px;font-size:12px;color:var(--muted)}
.grid b{display:block;font-size:15px;color:var(--ink);margin-top:2px}
.go{display:flex;align-items:center;justify-content:center;height:48px;border-radius:10px;background:var(--ink);color:#fff;font-weight:800;font-size:14px;margin-top:12px}
.err{color:#9a2b2b;font-size:14px;font-weight:600;margin:0}

section{padding:46px 0}
h2.t{font-size:28px;letter-spacing:-1px;margin:0 0 8px}
.lead{color:var(--muted);line-height:1.6;max-width:640px;margin:0 0 22px}
.tw{overflow-x:auto;border:1px solid var(--line);border-radius:12px}
table{width:100%;border-collapse:collapse;font-size:14px;min-width:520px}
th,td{padding:13px 16px;text-align:left;border-bottom:1px solid var(--line)}
th{background:var(--soft);font-size:13px}tr:last-child td{border-bottom:0}
details{border-bottom:1px solid var(--line);padding:16px 0}
summary{font-weight:750;cursor:pointer;font-size:16px}
details p{color:var(--muted);line-height:1.65;margin:10px 0 0;max-width:720px}
.aviso{background:var(--soft);padding:22px 0;font-size:12px;color:var(--muted);line-height:1.6}
footer{background:#f3f6f5;padding:18px 0;color:#687583;font-size:11px}
footer .container{display:flex;justify-content:space-between;gap:15px;flex-wrap:wrap}
footer nav{display:flex;gap:16px;flex-wrap:wrap}footer a:hover{color:var(--gd)}
@media(max-width:850px){.hero-in{grid-template-columns:1fr;gap:28px}.hero{padding:36px 0 40px}.big{font-size:30px}section{padding:34px 0}}
@media(max-width:480px){.card{padding:20px}.grid{grid-template-columns:1fr}footer .container{flex-direction:column}}
</style>
</head>
<body>
{% macro marca(tam=34) -%}
{%- if SITE.logo -%}<img src="{{ SITE.logo }}" alt="{{ SITE.nome }}" height="{{ tam }}" style="height:{{ tam }}px">{%- else -%}<span class="mark">⌂</span>Onde<span class="accent">Imóveis</span>{%- endif -%}
{%- endmacro %}
<header><div class="container nav"><a class="logo" href="{{ url_for('home') }}" aria-label="{{ SITE.nome }} — página inicial">{{ marca(66) }}</a>
<nav class="menu" aria-label="Principal"><a href="{{ url_for('buscar', finalidade='venda') }}">Comprar</a><a href="{{ url_for('buscar', finalidade='aluguel') }}">Alugar</a><a href="{{ url_for('imobiliarias') }}">Imobiliárias</a><a href="{{ url_for('guia') }}">Guia</a><a href="{{ url_for('anuncie') }}">Anuncie seu imóvel</a>{% if tem_rota('para_empresas') %}<a href="{{ url_for('para_empresas') }}">Para empresas</a>{% endif %}</nav>
<div class="actions"><a class="anunciar" href="{{ url_for('cadastrar', tipo='imobiliaria') }}">Anunciar</a><a class="near" href="{{ url_for('perto_de_mim') }}">⌖ Perto de mim</a><a href="{{ url_for('favoritos') }}" aria-label="Favoritos">♡</a><a href="{{ url_for('painel') if usuario else url_for('entrar') }}">♙ {{ usuario.nome.split()[0] if usuario else 'Entrar' }}</a></div><button class="burger" id="burger" type="button" aria-label="Abrir menu" aria-expanded="false" aria-controls="mnav"><span></span><span></span><span></span></button></div>
<nav class="mnav" id="mnav" aria-label="Menu"><a href="{{ url_for('buscar', finalidade='venda') }}">Comprar</a><a href="{{ url_for('buscar', finalidade='aluguel') }}">Alugar</a><a href="{{ url_for('imobiliarias') }}">Imobiliárias</a><a href="{{ url_for('guia') }}">Guia</a><a href="{{ url_for('anuncie') }}">Anuncie seu imóvel</a>{% if tem_rota('para_empresas') %}<a href="{{ url_for('para_empresas') }}">Para empresas</a>{% endif %}<hr><a href="{{ url_for('perto_de_mim') }}">⌖ Perto de mim</a><a href="{{ url_for('favoritos') }}">♡ Favoritos</a><a href="{{ url_for('painel') if usuario else url_for('entrar') }}">♙ {{ usuario.nome.split()[0] if usuario else 'Entrar' }}</a></nav></header>

<main>
<section class="hero"><div class="container hero-in">
  <div>
    <h1>Descubra até quanto de imóvel cabe no seu Minha Casa Minha Vida</h1>
    <p>Informe a renda da família e veja sua faixa, a parcela que cabe no orçamento e os imóveis dentro do limite do programa.</p>
    <ul class="facts">
      <li>Renda familiar de até {{ topo }} por mês</li>
      <li>Subsídio de até {{ subsidio }} nas faixas 1 e 2</li>
      <li>Até {{ anos }} anos para pagar</li>
    </ul>
  </div>
  <div class="card">
    <h2>Simule em 10 segundos</h2>
    <form id="sim" novalidate>
      <div class="row"><label for="renda">Renda familiar por mês</label>
        <div class="inp"><span>R$</span><input id="renda" name="renda" inputmode="numeric" autocomplete="off" placeholder="4.500" value="{{ renda0 }}"></div></div>
      <div class="row"><label for="entrada">Entrada ou FGTS que você tem <span class="hint">(opcional)</span></label>
        <div class="inp"><span>R$</span><input id="entrada" name="entrada" inputmode="numeric" autocomplete="off" placeholder="0" value="{{ entrada0 }}"></div></div>
      <button class="btn" id="go" type="submit">Simular</button>
    </form>
    <div class="res" id="res" aria-live="polite"></div>
  </div>
</div></section>

<section><div class="container">
  <h2 class="t">As faixas do programa</h2>
  <p class="lead">Quanto menor a renda, maior o subsídio. Valores de referência atualizados em {{ atualizado }}.</p>
  <div class="tw"><table>
    <thead><tr><th>Faixa</th><th>Renda familiar</th><th>Imóvel de até</th><th>Subsídio</th></tr></thead>
    <tbody>{% for r in regras %}<tr><td><b>Faixa {{ r.faixa }}</b></td><td>até {{ r.renda }}</td><td>{{ r.teto }}</td><td>{{ r.subsidio }}</td></tr>{% endfor %}</tbody>
  </table></div>
  <p class="small" style="margin-top:12px">Nas faixas 1 e 2 o valor máximo do imóvel depende da cidade (de {{ teto_min }} a {{ teto_max }}).</p>
</div></section>

<section style="background:var(--soft)"><div class="container">
  <h2 class="t">Perguntas frequentes</h2>
  <div style="margin-top:14px">{% for q, a in faq %}<details><summary>{{ q }}</summary><p>{{ a }}</p></details>{% endfor %}</div>
</div></section>

<div class="aviso"><div class="container">O {{ SITE.nome }} não é a Caixa nem faz parte do governo. Esta simulação é uma estimativa e não garante aprovação: juros, subsídio e valor final dependem da análise de crédito da Caixa ou do banco operador. Regras e valores do programa podem mudar.</div></div>
</main>

<footer><div class="container"><span>© {{ ano }} {{ SITE.nome }}</span><nav aria-label="Rodapé"><a href="{{ url_for('buscar') }}">Imóveis</a><a href="{{ url_for('imobiliarias') }}">Imobiliárias</a>{% if tem_rota('para_empresas') %}<a href="{{ url_for('para_empresas') }}">Para empresas</a>{% endif %}<a href="{{ url_for('anuncie') }}">Anunciar imóvel</a><a href="{{ url_for('guia') }}">Guia</a></nav></div></footer>

<script>
(function(){
var b=document.getElementById('burger'),m=document.getElementById('mnav');
function set(o){m.classList.toggle('on',o);b.setAttribute('aria-expanded',o);b.setAttribute('aria-label',o?'Fechar menu':'Abrir menu')}
b.onclick=function(){set(!m.classList.contains('on'))};
m.addEventListener('click',function(e){if(e.target.tagName==='A')set(false)});
document.addEventListener('keydown',function(e){if(e.key==='Escape')set(false)});
window.addEventListener('resize',function(){if(window.innerWidth>700)set(false)});

var f=document.getElementById('sim'),res=document.getElementById('res'),btn=document.getElementById('go');
var API={{ api|tojson }},BUSCA={{ busca|tojson }};
function brl(n){return 'R$ '+Math.round(n).toLocaleString('pt-BR')}
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
function cel(l,v){var d=el('div',null,l);d.appendChild(el('b',null,v));return d}
function erro(msg){res.replaceChildren(el('p','err',msg))}
function mostra(r){
  var o=el('div','out');
  if(!r.no_programa){
    o.appendChild(el('p','small','A renda informada passa do limite do programa ('+brl(r.limite_renda)+'). Você ainda pode financiar pelo crédito imobiliário comum, que não tem teto de renda.'));
    res.replaceChildren(o);return}
  o.appendChild(el('span','chip','Faixa '+r.faixa));
  o.appendChild(el('div','big','Imóveis de até '+brl(r.imovel_ate)));
  o.appendChild(el('p','small',r.limitado_pelo_teto?'Você já chega ao valor máximo da sua faixa.':'Estimativa com parcela de até 30% da renda.'));
  var g=el('div','grid');
  g.appendChild(cel('Parcela máxima',brl(r.parcela_max)+'/mês'));
  g.appendChild(cel('Prazo','até '+r.prazo_anos+' anos'));
  g.appendChild(cel('Financiamento estimado',brl(r.financiavel)));
  g.appendChild(cel('Subsídio',r.subsidio_max?'até '+brl(r.subsidio_max):'não tem'));
  o.appendChild(g);
  if(r.depende_cidade)o.appendChild(el('p','small','Em cidades menores o limite da faixa pode ser menor (a partir de '+brl(r.teto_min)+').'));
  var a=el('a','go','Ver imóveis até '+brl(r.busca_preco));a.href=BUSCA+'?finalidade=venda&preco_max='+r.busca_preco;o.appendChild(a);
  res.replaceChildren(o)}
function run(){
  var renda=document.getElementById('renda').value,ent=document.getElementById('entrada').value;
  if(!/\d/.test(renda)){erro('Informe a renda familiar por mês.');return}
  btn.disabled=true;
  fetch(API+'?renda='+encodeURIComponent(renda)+'&entrada='+encodeURIComponent(ent),{headers:{'Accept':'application/json'}})
    .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j}})})
    .then(function(x){x.ok?mostra(x.j):erro(x.j.erro||'Não consegui simular agora.')})
    .catch(function(){erro('Não consegui simular agora. Tente de novo.')})
    .then(function(){btn.disabled=false})}
f.addEventListener('submit',function(e){e.preventDefault();run()});
if(document.getElementById('renda').value)run();
})();
</script>
</body>
</html>
"""


# ═══════════════════════════════════════════════════════════════
# ROTAS
# ═══════════════════════════════════════════════════════════════
@bp.route("/minha-casa-minha-vida", strict_slashes=False)
def pagina():
    if request.path != url_for("mcmv.pagina"):       # /minha-casa-minha-vida/ -> 301 para a versão sem barra (canonical único)
        return redirect(url_for("mcmv.pagina", **request.args), 301)
    faq = _faq()
    topo, f12 = REGRAS[-1]["renda_max"], REGRAS[1]       # f12: faixas 1 e 2 (subsídio e teto por cidade)
    renda0, entrada0 = _num(request.args.get("renda")), _num(request.args.get("entrada"))
    jsonld = [{"@context": "https://schema.org", "@type": "FAQPage",
               "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq]}]
    return render_template_string(
        PAGINA, faq=faq, topo=_brl(topo), anos=PRAZO_MESES // 12, subsidio=_brl(f12["subsidio"]),
        atualizado=ATUALIZADO_EM, teto_min=_brl(f12["teto_min"]), teto_max=_brl(f12["teto"]),
        regras=[dict(faixa=r["faixa"], renda=_brl(r["renda_max"]), teto=("até " + _brl(r["teto"])),
                     subsidio=("até " + _brl(r["subsidio"]) if r["subsidio"] else "não tem")) for r in REGRAS],
        renda0=f"{renda0:.0f}" if renda0 and 0 < renda0 <= 1_000_000 else "",
        entrada0=f"{entrada0:.0f}" if entrada0 and 0 < entrada0 <= 100_000_000 else "",
        api=url_for("mcmv.api_simular"), busca=url_for("buscar"),
        seo_desc=f"Simule o Minha Casa Minha Vida: veja sua faixa, a parcela e até quanto de imóvel cabe. Renda de até {_brl(topo)}, "
                 f"subsídio de até {_brl(f12['subsidio'])} e até {PRAZO_MESES // 12} anos para pagar.",
        seo_jsonld=jsonld)


@bp.route("/api/mcmv/simular")
def api_simular():
    renda, entrada = _num(request.args.get("renda")), _num(request.args.get("entrada")) or 0.0
    if not renda or renda <= 0:
        return jsonify(erro="Informe a renda familiar por mês."), 400
    if renda > 1_000_000 or entrada > 100_000_000:
        return jsonify(erro="Confira os valores informados."), 400
    resp = jsonify(simular(renda, entrada))
    resp.headers["Cache-Control"] = "no-store"
    return resp


def init(app):
    """Liga o módulo ao app (chame uma vez, no fim do app.py)."""
    app.register_blueprint(bp)
