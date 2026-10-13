"""
OndeImóveis — portal imobiliário (Flask + PostgreSQL).

Arquivo único. Configuração 100% por variáveis de ambiente (.env):
  DATABASE_URL, SECRET_KEY, BASE_URL, UPLOAD_DIR, ADMIN_EMAIL, ADMIN_SENHA, SEED_DEMO,
  PAGBANK_TOKEN, PAGBANK_SANDBOX, PAGBANK_WEBHOOK_TOKEN, PRECO_PROFISSIONAL, DEBUG, PORT
  Tarefas agendadas (cron externo, 1x/dia): TAREFAS_TOKEN — chame /tarefas/renovacao?t=<token> (ou header X-Tarefas-Token); sem ele a rota responde 403
  Chatbot (Groq): GROQ_API_KEY, GROQ_API_KEY_2 (reserva), GROQ_MODEL (opcional)
  E-mail (Zoho): SMTP_USER (e-mail completo), SMTP_SENHA (senha de aplicativo), EMAIL_AVISOS, SMTP_HOST (padrão smtp.zoho.com), SMTP_PORT (587)
  Verificação de e-mail no cadastro (código de 6 dígitos): liga sozinha quando o SMTP está configurado; VERIFICAR_EMAIL=0 desliga
Fotos: ficam num volume (UPLOAD_DIR) e são servidas por /assets/<id>?w=640 (redimensiona e guarda em cache).
Os templates (templates/*.html) são HTML único, com CSS e JS no mesmo arquivo.
Guia/blog: /guia (público, SEO) e /admin/guia (editor, categorias, autores). Tabelas guia_* criadas por init_db (só CREATE IF NOT EXISTS).
"""
import os, re, io, json, math, time, hmac, uuid, glob, hashlib, secrets, logging, unicodedata, smtplib, ssl, threading, shutil, calendar
from email.message import EmailMessage
from email.utils import format_datetime
from html import unescape as html_unescape
from html.parser import HTMLParser
from datetime import date, datetime, timedelta, timezone
from functools import wraps, lru_cache
from urllib.parse import quote, urlparse
from xml.sax.saxutils import escape as xml_escape
from zoneinfo import ZoneInfo

import requests
import psycopg2
import psycopg2.extras
import psycopg2.pool
from PIL import Image, ImageDraw, ImageOps
from flask import (Flask, make_response, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from markupsafe import Markup, escape as html_escape
from werkzeug.datastructures import MultiDict
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

try:                                    # .env opcional (no Docker as variáveis já vêm do ambiente)
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ═══════════════════════════════════════════════════════════════
# 1. CONFIGURAÇÃO
# ═══════════════════════════════════════════════════════════════

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _bool(nome, padrao=False):
    v = os.environ.get(nome)
    return padrao if v is None else v.strip().lower() in ("1", "true", "yes", "sim", "on")


DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
BASE_URL = os.environ.get("BASE_URL", "").strip().rstrip("/")
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(BASE_DIR, "data", "uploads"))
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip().lower()
ADMIN_SENHA = os.environ.get("ADMIN_SENHA", "")
SEED_DEMO = _bool("SEED_DEMO")
PAGBANK_TOKEN = os.environ.get("PAGBANK_TOKEN", "").strip()
PAGBANK_SANDBOX = _bool("PAGBANK_SANDBOX", True)
PAGBANK_WEBHOOK_TOKEN = os.environ.get("PAGBANK_WEBHOOK_TOKEN", "").strip()
PRECO_PROFISSIONAL = float(os.environ.get("PRECO_PROFISSIONAL", "89.90"))
# Cobrança recorrente no cartão (Checkout Recorrente do PagBank). Se o PagBank recusar criar o checkout recorrente, cai no avulso.
PAGBANK_RECORRENTE = _bool("PAGBANK_RECORRENTE", True)
PAGBANK_CONFIRMAR = _bool("PAGBANK_CONFIRMAR", True)     # confirma na API do PagBank antes de liberar o Pro (deixe ligado em produção)
# Folga depois da data da próxima cobrança, para o PagBank tentar de novo o cartão sem derrubar o Pro no mesmo dia.
RECORRENCIA_TOLERANCIA_DIAS = int(os.environ.get("RECORRENCIA_TOLERANCIA_DIAS", "3"))
TZ = ZoneInfo(os.environ.get("TZ_PORTAL", "America/Sao_Paulo"))
# Avisos por e-mail (Gmail + senha de app de 16 dígitos). Sem SMTP_USER/SMTP_SENHA, os avisos ficam desligados.
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.zoho.com").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_SENHA = os.environ.get("SMTP_SENHA", "").replace(" ", "")
EMAIL_AVISOS = os.environ.get("EMAIL_AVISOS", "").strip() or ADMIN_EMAIL
TAREFAS_TOKEN = os.environ.get("TAREFAS_TOKEN", "").strip()      # segredo das rotas /tarefas/* (cron externo)
# Sem SMTP não há como mandar o código: a verificação fica desligada para ninguém ficar trancado fora.
VERIFICAR_EMAIL = _bool("VERIFICAR_EMAIL", bool(SMTP_USER and SMTP_SENHA))
CONSENTIMENTO_VERSAO = "2026-10-10"      # mude quando alterar o texto do aceite (fica gravado junto da data)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ondeimoveis")

TIPOS_IMOVEL = {"apartamento": "Apartamento", "casa": "Casa", "cobertura": "Cobertura",
                "comercial": "Comercial", "terreno": "Terreno"}
FINALIDADES = {"venda": "Venda", "aluguel": "Aluguel"}
STATUS_IMOVEL = {"publicado": "Publicado", "pendente": "Em análise", "pausado": "Pausado", "rejeitado": "Rejeitado"}
PLANOS = {"gratis": "Grátis", "profissional": "Profissional"}
LIMITES = {"gratis": {"imoveis": 3, "fotos": 8, "destaques": 0}, "profissional": {"imoveis": 50, "fotos": 20, "destaques": 3}}
# Períodos do Profissional. É só o "seed" da tabela planos_pro: depois que ela existe, preço e liga/desliga são editados em /admin/planos.
# (codigo, nome, meses, fator sobre o preço mensal, nasce ativo?). Só o mensal nasce ligado: os outros começam desligados, com preço
# provisório (desconto de exemplo), para ninguém comprar por um valor que você ainda não conferiu.
PLANOS_PRO_PADRAO = (("mensal", "Mensal", 1, 1.00, True), ("trimestral", "Trimestral", 3, 0.95, False),
                     ("semestral", "Semestral", 6, 0.90, False), ("anual", "Anual", 12, 0.85, False))
RENOVAR_ANTES_DIAS = 10     # o Pro ativo pode renovar (pagamento avulso de 30 dias) quando faltarem tantos dias ou menos
TIPOS_CONTA = {"visitante": "Visitante", "proprietario": "Proprietário", "corretor": "Corretor", "imobiliaria": "Imobiliária"}
TIPOS_TENANT = ("proprietario", "corretor", "imobiliaria")
CARACTERISTICAS = ["Suíte", "Piscina", "Sacada", "Salão de festas", "Academia", "Portaria 24h", "Churrasqueira",
                   "Elevador", "Playground", "Mobiliado", "Ar-condicionado", "Armários embutidos", "Quintal", "Aceita pets"]
CATEGORIAS_PROXIMO = {"escola": "Escolas", "metro": "Metrô", "mercado": "Mercados", "farmacia": "Farmácias",
                      "transporte": "Transporte público", "parque": "Parques", "hospital": "Hospitais",
                      "restaurante": "Restaurantes", "academia": "Academias"}
ICONES_PROXIMO = {"escola": "⌂", "metro": "◉", "mercado": "▣", "farmacia": "♧", "transporte": "▤",
                  "parque": "❦", "hospital": "✚", "restaurante": "☕", "academia": "♞"}

app = Flask(__name__)
app.config.update(
    MAX_CONTENT_LENGTH=60 * 1024 * 1024,           # upload (várias fotos)
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=BASE_URL.startswith("https"),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    JSON_AS_ASCII=False, TEMPLATES_AUTO_RELOAD=_bool("DEBUG"),
)
os.makedirs(os.path.join(UPLOAD_DIR, "originais"), exist_ok=True)
os.makedirs(os.path.join(UPLOAD_DIR, "cache"), exist_ok=True)
# atrás do Traefik/Dokploy: respeita X-Forwarded-Proto/Host (URLs https corretas no sitemap, canonical e redirects)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def _chave_secreta():
    k = os.environ.get("SECRET_KEY", "").strip()
    if k:
        return k
    arq = os.path.join(UPLOAD_DIR, ".secret_key")      # persiste no volume → sessões sobrevivem a reinícios
    try:
        if os.path.exists(arq):
            return open(arq).read().strip()
        k = secrets.token_hex(32)
        open(arq, "w").write(k)
        log.warning("SECRET_KEY não definida: gerei uma e guardei em %s. Defina SECRET_KEY no .env.", arq)
        return k
    except OSError:
        return secrets.token_hex(32)


app.secret_key = _chave_secreta()

# ═══════════════════════════════════════════════════════════════
# 2. BANCO (PostgreSQL) — 1 conexão por requisição, commit no fim
# ═══════════════════════════════════════════════════════════════

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(1, int(os.environ.get("DB_POOL_MAX", "12")), DATABASE_URL)
    return _pool


def _conn():
    if "db" not in g:
        g.db = _get_pool().getconn()
    return g.db


@app.teardown_appcontext
def _fechar_db(exc):
    c = g.pop("db", None)
    if c is not None:
        try:
            if exc is None:
                c.commit()
            else:
                c.rollback()
        except Exception:
            c.rollback()
        _get_pool().putconn(c)


def _cur():
    return _conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def query_all(sql, params=None):
    with _cur() as c:
        c.execute(sql, params)
        return [dict(r) for r in c.fetchall()]


def query_one(sql, params=None):
    with _cur() as c:
        c.execute(sql, params)
        r = c.fetchone()
        return dict(r) if r else None


def execute(sql, params=None):
    with _cur() as c:
        c.execute(sql, params)
        return c.rowcount


def execute_returning(sql, params=None):
    with _cur() as c:
        c.execute(sql, params)
        r = c.fetchone()
        return next(iter(r.values())) if r else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS usuarios (
  id SERIAL PRIMARY KEY, nome TEXT NOT NULL, email TEXT NOT NULL UNIQUE, senha_hash TEXT NOT NULL,
  tipo TEXT NOT NULL DEFAULT 'visitante', telefone TEXT, cidade TEXT, criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS arquivos (
  id TEXT PRIMARY KEY, nome TEXT, mime TEXT, tamanho BIGINT NOT NULL DEFAULT 0, largura INT, altura INT,
  pasta TEXT, criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS tenants (
  id SERIAL PRIMARY KEY, usuario_id INT NOT NULL UNIQUE REFERENCES usuarios(id) ON DELETE CASCADE,
  nome TEXT NOT NULL, slug TEXT NOT NULL UNIQUE, tipo TEXT NOT NULL, descricao TEXT, creci TEXT, telefone TEXT,
  whatsapp TEXT, horario TEXT, cidade TEXT, uf TEXT, instagram TEXT, facebook TEXT, linkedin TEXT, anos_mercado INT,
  logo_file_id TEXT, cor_primaria TEXT, verificada BOOLEAN NOT NULL DEFAULT FALSE, status TEXT NOT NULL DEFAULT 'ativo',
  plano TEXT NOT NULL DEFAULT 'gratis', assinatura_status TEXT, plano_vencimento DATE,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS imoveis (
  id SERIAL PRIMARY KEY, tenant_id INT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  titulo TEXT NOT NULL, slug TEXT NOT NULL UNIQUE, finalidade TEXT NOT NULL, tipo TEXT NOT NULL,
  preco NUMERIC(14,2) NOT NULL DEFAULT 0, condominio NUMERIC(12,2), iptu NUMERIC(12,2),
  cidade TEXT NOT NULL, cidade_slug TEXT NOT NULL, uf TEXT, bairro TEXT NOT NULL, bairro_slug TEXT NOT NULL,
  dormitorios INT NOT NULL DEFAULT 0, suites INT NOT NULL DEFAULT 0, banheiros INT NOT NULL DEFAULT 0,
  vagas INT NOT NULL DEFAULT 0, area NUMERIC(10,2), descricao TEXT, caracteristicas TEXT, whatsapp TEXT,
  lat DOUBLE PRECISION, lng DOUBLE PRECISION, status TEXT NOT NULL DEFAULT 'pendente', motivo_rejeicao TEXT,
  destaque BOOLEAN NOT NULL DEFAULT FALSE, criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  atualizado_em TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS ix_imoveis_busca ON imoveis (status, finalidade, tipo, preco);
CREATE INDEX IF NOT EXISTS ix_imoveis_tenant ON imoveis (tenant_id);
CREATE TABLE IF NOT EXISTS imovel_fotos (
  id SERIAL PRIMARY KEY, imovel_id INT NOT NULL REFERENCES imoveis(id) ON DELETE CASCADE,
  file_id TEXT NOT NULL, ordem INT NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_fotos_imovel ON imovel_fotos (imovel_id, ordem);
CREATE TABLE IF NOT EXISTS imovel_proximos (
  id SERIAL PRIMARY KEY, imovel_id INT NOT NULL REFERENCES imoveis(id) ON DELETE CASCADE,
  categoria TEXT NOT NULL, nome TEXT NOT NULL, link TEXT, distancia_m INT, status TEXT NOT NULL DEFAULT 'pendente');
CREATE TABLE IF NOT EXISTS favoritos (
  usuario_id INT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
  imovel_id INT NOT NULL REFERENCES imoveis(id) ON DELETE CASCADE,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY (usuario_id, imovel_id));
CREATE TABLE IF NOT EXISTS eventos (
  id BIGSERIAL PRIMARY KEY, tenant_id INT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  imovel_id INT REFERENCES imoveis(id) ON DELETE CASCADE, usuario_id INT REFERENCES usuarios(id) ON DELETE SET NULL,
  tipo TEXT NOT NULL, detalhe TEXT, criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS ix_eventos_tenant ON eventos (tenant_id, tipo, criado_em);
CREATE INDEX IF NOT EXISTS ix_eventos_usuario ON eventos (usuario_id, tipo, criado_em);
CREATE TABLE IF NOT EXISTS assinaturas (
  id SERIAL PRIMARY KEY, tenant_id INT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, plano TEXT NOT NULL,
  status TEXT NOT NULL, valor NUMERIC(10,2) NOT NULL DEFAULT 0, vencimento DATE, provedor_pagamento TEXT,
  referencia_externa TEXT, criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(), atualizado_em TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS pagamentos (
  id SERIAL PRIMARY KEY, tenant_id INT REFERENCES tenants(id) ON DELETE SET NULL, provedor TEXT NOT NULL,
  evento_id TEXT NOT NULL, referencia TEXT, status TEXT NOT NULL, valor NUMERIC(10,2), payload TEXT,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE (provedor, evento_id));
CREATE TABLE IF NOT EXISTS newsletter (email TEXT PRIMARY KEY, criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW());
ALTER TABLE imoveis ADD COLUMN IF NOT EXISTS rua TEXT;
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS email_verificado_em TIMESTAMPTZ;
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS telefone_verificado_em TIMESTAMPTZ;
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS consentimento_em TIMESTAMPTZ;
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS consentimento_versao TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS template TEXT;
ALTER TABLE imoveis ADD COLUMN IF NOT EXISTS destaque_anunciante BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE imoveis ADD COLUMN IF NOT EXISTS pausado_plano BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE imoveis ADD COLUMN IF NOT EXISTS aceita_mcmv BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS ix_imoveis_mcmv ON imoveis (cidade_slug, preco) WHERE aceita_mcmv;
CREATE INDEX IF NOT EXISTS ix_imoveis_pausado_plano ON imoveis (tenant_id) WHERE pausado_plano;
ALTER TABLE assinaturas ADD COLUMN IF NOT EXISTS recorrente BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE assinaturas ADD COLUMN IF NOT EXISTS plano_externo_id TEXT;
ALTER TABLE assinaturas ADD COLUMN IF NOT EXISTS assinatura_externa_id TEXT;
ALTER TABLE assinaturas ADD COLUMN IF NOT EXISTS proxima_cobranca DATE;
CREATE TABLE IF NOT EXISTS avisos_assinatura (
  id SERIAL PRIMARY KEY, tenant_id INT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, tipo TEXT NOT NULL,
  vencimento DATE NOT NULL, enviado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE (tenant_id, tipo, vencimento));
CREATE TABLE IF NOT EXISTS verificacoes (
  id BIGSERIAL PRIMARY KEY, usuario_id INT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
  codigo_hash TEXT NOT NULL, expira_em TIMESTAMPTZ NOT NULL, tentativas INT NOT NULL DEFAULT 0,
  usado BOOLEAN NOT NULL DEFAULT FALSE, criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS ix_verif_usuario ON verificacoes (usuario_id, id DESC);
ALTER TABLE assinaturas ADD COLUMN IF NOT EXISTS plano_codigo TEXT;
ALTER TABLE assinaturas ADD COLUMN IF NOT EXISTS meses INT;
CREATE TABLE IF NOT EXISTS planos_pro (
  codigo TEXT PRIMARY KEY, nome TEXT NOT NULL, meses INT NOT NULL CHECK (meses > 0),
  valor NUMERIC(10,2) NOT NULL CHECK (valor > 0), ativo BOOLEAN NOT NULL DEFAULT TRUE);
"""


# Chatbot — só CREATE ... IF NOT EXISTS: nunca apaga nem recria nada.
SCHEMA_CHAT = """
CREATE TABLE IF NOT EXISTS chatbot_config (
  chave TEXT PRIMARY KEY, valor TEXT NOT NULL DEFAULT '', atualizado_em TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS chatbot_config_historico (
  id BIGSERIAL PRIMARY KEY, chave TEXT NOT NULL, valor TEXT, usuario_id INT, criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS ix_chatcfg_hist ON chatbot_config_historico (chave, id DESC);
CREATE TABLE IF NOT EXISTS chat_sessoes (
  id TEXT PRIMARY KEY, modo TEXT NOT NULL, usuario_id INT, perfil TEXT, finalizada BOOLEAN NOT NULL DEFAULT FALSE,
  ip_hash TEXT, user_agent TEXT, origem TEXT, criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(), atualizado_em TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS ix_chat_sessoes_data ON chat_sessoes (criado_em DESC);
CREATE TABLE IF NOT EXISTS chat_mensagens (
  id BIGSERIAL PRIMARY KEY, sessao_id TEXT NOT NULL REFERENCES chat_sessoes(id), papel TEXT NOT NULL,
  tipo TEXT NOT NULL DEFAULT 'texto', texto TEXT, meta TEXT, criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS ix_chat_msgs_sessao ON chat_mensagens (sessao_id, id);
CREATE TABLE IF NOT EXISTS chat_respostas (
  id BIGSERIAL PRIMARY KEY, sessao_id TEXT NOT NULL REFERENCES chat_sessoes(id), campo TEXT NOT NULL, valor TEXT, rotulo TEXT,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS ix_chat_resp_sessao ON chat_respostas (sessao_id, id);
CREATE TABLE IF NOT EXISTS chat_recomendacoes (
  id BIGSERIAL PRIMARY KEY, sessao_id TEXT NOT NULL REFERENCES chat_sessoes(id), imovel_id INT, imovel_slug TEXT, titulo TEXT,
  preco NUMERIC(14,2), score INT, criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS ix_chat_rec_sessao ON chat_recomendacoes (sessao_id, id);
"""


# Leads — pessoas cadastradas que pediram contato (WhatsApp/telefone/visita). Guarda uma "foto" dos dados do
# imóvel/anunciante, então o lead continua completo mesmo que o anúncio seja apagado depois.
SCHEMA_LEADS = """
CREATE TABLE IF NOT EXISTS leads (
  id BIGSERIAL PRIMARY KEY,
  usuario_id INT REFERENCES usuarios(id) ON DELETE SET NULL,
  tenant_id INT REFERENCES tenants(id) ON DELETE SET NULL,
  imovel_id INT REFERENCES imoveis(id) ON DELETE SET NULL,
  origem TEXT NOT NULL, canal TEXT NOT NULL,
  nome TEXT, email TEXT, telefone TEXT, cidade TEXT,
  tenant_nome TEXT, imovel_titulo TEXT, finalidade TEXT, tipo_imovel TEXT, preco NUMERIC(14,2),
  cidade_imovel TEXT, bairro_imovel TEXT, quartos INT,
  detalhe TEXT, cookies TEXT, ip_hash TEXT, user_agent TEXT,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS ix_leads_data ON leads (criado_em DESC);
CREATE INDEX IF NOT EXISTS ix_leads_tenant ON leads (tenant_id, criado_em DESC);
CREATE INDEX IF NOT EXISTS ix_leads_usuario ON leads (usuario_id, canal, criado_em);
ALTER TABLE leads ADD COLUMN IF NOT EXISTS email_verificado BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS telefone_verificado BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS consentimento_em TIMESTAMPTZ;
-- importa UMA vez os contatos antigos (eventos de logados) para a tabela de leads
INSERT INTO leads (usuario_id, tenant_id, imovel_id, origem, canal, nome, email, telefone, cidade, tenant_nome,
                   imovel_titulo, finalidade, tipo_imovel, preco, cidade_imovel, bairro_imovel, quartos, detalhe, criado_em)
SELECT e.usuario_id, e.tenant_id, e.imovel_id, 'imovel', e.tipo, u.nome, u.email, u.telefone, u.cidade, t.nome,
       i.titulo, i.finalidade, i.tipo, i.preco, i.cidade, i.bairro, i.dormitorios, e.detalhe, e.criado_em
  FROM eventos e JOIN usuarios u ON u.id = e.usuario_id JOIN tenants t ON t.id = e.tenant_id
  LEFT JOIN imoveis i ON i.id = e.imovel_id
 WHERE e.tipo IN ('whatsapp','visita') AND e.usuario_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM leads);
"""


# Guia / blog — só CREATE ... IF NOT EXISTS: nunca apaga nem altera nada que já existe (igual ao script de migração).
SCHEMA_GUIA = """
CREATE TABLE IF NOT EXISTS guia_autores (
  id SERIAL PRIMARY KEY, nome TEXT NOT NULL, slug TEXT NOT NULL, cargo TEXT, bio TEXT, foto TEXT,
  usuario_id INT REFERENCES usuarios(id) ON DELETE SET NULL, criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS guia_categorias (
  id SERIAL PRIMARY KEY, nome TEXT NOT NULL, slug TEXT NOT NULL, descricao TEXT, seo_titulo TEXT, seo_descricao TEXT,
  ordem INT NOT NULL DEFAULT 0, ativa BOOLEAN NOT NULL DEFAULT TRUE, criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS guia_tags (
  id SERIAL PRIMARY KEY, nome TEXT NOT NULL, slug TEXT NOT NULL, criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS guia_posts (
  id SERIAL PRIMARY KEY, titulo TEXT NOT NULL, slug TEXT NOT NULL, resumo TEXT,
  corpo TEXT NOT NULL DEFAULT '', corpo_formato TEXT NOT NULL DEFAULT 'html', tipo TEXT NOT NULL DEFAULT 'artigo',
  capa TEXT, capa_alt TEXT,
  categoria_id INT REFERENCES guia_categorias(id) ON DELETE SET NULL,
  autor_id INT REFERENCES guia_autores(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'rascunho' CHECK (status IN ('rascunho','agendado','publicado')),
  destaque BOOLEAN NOT NULL DEFAULT FALSE, ordem_destaque INT NOT NULL DEFAULT 0, publicado_em TIMESTAMPTZ,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(), atualizado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  seo_titulo TEXT, seo_descricao TEXT, noindex BOOLEAN NOT NULL DEFAULT FALSE,
  uf TEXT, cidade_slug TEXT, bairro_slug TEXT, tempo_leitura INT, visualizacoes INT NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS guia_post_tags (
  post_id INT NOT NULL REFERENCES guia_posts(id) ON DELETE CASCADE,
  tag_id INT NOT NULL REFERENCES guia_tags(id) ON DELETE CASCADE, PRIMARY KEY (post_id, tag_id));
CREATE TABLE IF NOT EXISTS guia_redirecionamentos (
  slug_antigo TEXT PRIMARY KEY, post_id INT NOT NULL REFERENCES guia_posts(id) ON DELETE CASCADE,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE UNIQUE INDEX IF NOT EXISTS ux_guia_autores_slug ON guia_autores (slug);
CREATE UNIQUE INDEX IF NOT EXISTS ux_guia_categorias_slug ON guia_categorias (slug);
CREATE UNIQUE INDEX IF NOT EXISTS ux_guia_tags_slug ON guia_tags (slug);
CREATE UNIQUE INDEX IF NOT EXISTS ux_guia_posts_slug ON guia_posts (slug);
CREATE INDEX IF NOT EXISTS ix_guia_posts_pub ON guia_posts (status, publicado_em DESC);
CREATE INDEX IF NOT EXISTS ix_guia_posts_cat ON guia_posts (categoria_id, publicado_em DESC);
CREATE INDEX IF NOT EXISTS ix_guia_posts_local ON guia_posts (cidade_slug, bairro_slug);
CREATE INDEX IF NOT EXISTS ix_guia_posts_destaque ON guia_posts (ordem_destaque, publicado_em DESC) WHERE status = 'publicado' AND destaque;
CREATE INDEX IF NOT EXISTS ix_guia_posts_busca ON guia_posts USING GIN
  (to_tsvector('portuguese', coalesce(titulo,'') || ' ' || coalesce(resumo,'') || ' ' || coalesce(corpo,'')));
CREATE INDEX IF NOT EXISTS ix_guia_post_tags_tag ON guia_post_tags (tag_id);
CREATE INDEX IF NOT EXISTS ix_guia_redir_post ON guia_redirecionamentos (post_id);
"""


def init_db():
    """Cria as tabelas (idempotente). Espera o Postgres subir; trava para só 1 worker migrar."""
    ultimo = None
    for _ in range(40):
        try:
            c = psycopg2.connect(DATABASE_URL)
            break
        except psycopg2.OperationalError as e:
            ultimo = e
            log.info("Aguardando o PostgreSQL…")
            time.sleep(1.5)
    else:
        raise RuntimeError(f"Não consegui conectar ao PostgreSQL: {ultimo}")
    try:
        with c.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(727274)")
            cur.execute(SCHEMA)
            cur.execute(SCHEMA_CHAT)
            cur.execute(SCHEMA_LEADS)
            cur.execute(SCHEMA_GUIA)
            for cod, nome, meses, fator, ativo in PLANOS_PRO_PADRAO:     # só cria o que falta: nunca sobrescreve o que o admin editou
                cur.execute("INSERT INTO planos_pro (codigo, nome, meses, valor, ativo) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (codigo) DO NOTHING",
                            (cod, nome, meses, round(PRECO_PROFISSIONAL * meses * fator, 2), ativo))
            cur.execute("SELECT pg_advisory_unlock(727274)")
        c.commit()
    finally:
        c.close()


# ═══════════════════════════════════════════════════════════════
# 3. AUXILIARES (texto, números, slugs, planos) e filtros do Jinja
# ═══════════════════════════════════════════════════════════════

def agora():
    return datetime.now(timezone.utc)


def hoje():
    return datetime.now(TZ).date()


def sanitize_input(txt):
    """Tira tags HTML e caracteres de controle (o Jinja já escapa na saída; isto é uma 2ª camada)."""
    txt = re.sub(r"<[^>]*>", "", str(txt or ""))
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", txt).strip()


def gerar_slug(txt):
    txt = unicodedata.normalize("NFKD", str(txt or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", txt.lower()).strip("-")[:80] or "item"


def slug_unico(tabela, base):
    assert tabela in ("imoveis", "tenants")
    slug, n = base, 1
    while query_one(f"SELECT 1 FROM {tabela} WHERE slug = %s", (slug,)):
        n += 1
        slug = f"{base}-{n}"
    return slug


def so_digitos(txt):
    return re.sub(r"\D", "", str(txt or ""))


def wa_link(numero, texto=""):
    d = so_digitos(numero)
    if len(d) in (10, 11):
        d = "55" + d
    return f"https://wa.me/{d}" + (f"?text={quote(texto)}" if texto else "")


def num_br(v):
    """'850.000,50' | '850000.5' | '1.200' → float (ou None)."""
    if v is None:
        return None
    s = re.sub(r"[^\d,.\-]", "", str(v).replace("R$", ""))
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") > 1 or re.fullmatch(r"-?\d{1,3}\.\d{3}", s):
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def seguro_next(destino):
    """Só aceita caminhos internos (evita open-redirect)."""
    return bool(destino) and destino.startswith("/") and not destino.startswith("//") and "\\" not in destino


def _fmt_num(v, casas=0):
    if v is None or v == "":
        return "—"
    v = float(v)
    s = f"{v:,.{casas}f}" if casas else f"{v:,.0f}"
    if casas and s.endswith("0" * casas) and v == int(v) and casas == 2 and False:
        pass
    if not casas and abs(v - round(v)) > 1e-9:
        s = f"{v:,.2f}".rstrip("0").rstrip(".")
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def preco_txt(r):
    return "R$ " + _fmt_num(r["preco"]) + ("/mês" if r["finalidade"] == "aluguel" else "")


def _local(dt):
    if isinstance(dt, datetime):
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(TZ)
    return dt


@app.template_filter("num")
def f_num(v, casas=0):
    return _fmt_num(v, casas)


@app.template_filter("preco")
def f_preco(r):
    return preco_txt(r)


@app.template_filter("iniciais")
def f_iniciais(nome):
    p = [x for x in re.split(r"\s+", str(nome or "").strip()) if x and x[0].isalnum()]
    return ("".join(x[0] for x in p[:2]) or "?").upper()


@app.template_filter("fone")
def f_fone(t):
    d = so_digitos(t)
    if len(d) == 11:
        return f"({d[:2]}) {d[2:7]}-{d[7:]}"
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    return t or ""


@app.template_filter("data")
def f_data(v, hora=False):
    if not v:
        return "—"
    v = _local(v)
    return v.strftime("%d/%m/%Y" + (" %H:%M" if hora and isinstance(v, datetime) else ""))


@app.template_filter("tempo")
def f_tempo(v):
    if not v:
        return ""
    v = _local(v)
    if not isinstance(v, datetime):
        return v.strftime("%d/%m")
    s = (datetime.now(TZ) - v).total_seconds()
    if s < 60:
        return "agora"
    if s < 3600:
        return f"há {int(s // 60)} min"
    if s < 86400:
        return f"há {int(s // 3600)} h"
    if s < 172800:
        return "ontem"
    return v.strftime("%d/%m")


# ═══════════════════════════════════════════════════════════════
# 3b. ARMAZENAMENTO DE FOTOS (volume em disco + /assets com redimensionamento)
# ═══════════════════════════════════════════════════════════════

LARGURAS = (120, 200, 240, 320, 480, 640, 960, 1200, 1600)
MIME_OK = {"JPEG", "PNG", "WEBP"}


FOTO_MAX_PX = 1280      # maior lado (px) das imagens guardadas em originais/ (fotos, logos, guia, demo)
FOTO_QUALIDADE = 80     # qualidade JPEG ao gravar
IMG_MAX_PIXELS = 100_000_000                 # recusa imagens acima disso (proteção contra "bomba de descompressão")
Image.MAX_IMAGE_PIXELS = IMG_MAX_PIXELS
# Processar imagem é o que mais gasta RAM. Poucas por vez (por processo): uploads simultâneos esperam na fila
# em vez de somarem os picos de memória. Ajuste com IMG_CONCORRENCIA no .env.
_IMG_SEM = threading.BoundedSemaphore(max(1, int(os.environ.get("IMG_CONCORRENCIA", "2"))))


class storage:
    """Imagens em UPLOAD_DIR/originais/<id>.jpg (máx. FOTO_MAX_PX). Variantes sob demanda em UPLOAD_DIR/cache."""
    orig = os.path.join(UPLOAD_DIR, "originais")
    cache = os.path.join(UPLOAD_DIR, "cache")

    @staticmethod
    def salvar_imagem(origem, nome="foto.jpg", pasta="imoveis", max_px=FOTO_MAX_PX, qualidade=FOTO_QUALIDADE):
        """origem: PIL.Image | bytes | arquivo aberto. Valida, corrige rotação, converte p/ JPEG e grava. Devolve o id."""
        with _IMG_SEM:
            return storage._salvar_imagem(origem, nome, pasta, max_px, qualidade)

    @staticmethod
    def _salvar_imagem(origem, nome, pasta, max_px, qualidade):
        if isinstance(origem, Image.Image):
            img = origem
        else:
            fonte = io.BytesIO(origem) if isinstance(origem, bytes) else origem
            if not (hasattr(fonte, "seekable") and fonte.seekable()):
                fonte = io.BytesIO(fonte.read())
            fonte.seek(0, os.SEEK_END)
            if fonte.tell() == 0:
                raise ValueError("arquivo vazio")
            fonte.seek(0)
            Image.open(fonte).verify()
            fonte.seek(0)
            img = Image.open(fonte)
            if img.format not in MIME_OK:
                raise ValueError("formato não suportado")
            if img.width * img.height > IMG_MAX_PIXELS:
                raise ValueError("imagem grande demais")
            if img.format == "JPEG":
                # o decodificador já entrega a foto reduzida (1/2, 1/4 ou 1/8): uma foto de 48 MP deixa de ocupar ~150 MB
                img.draft("RGB", (max_px, max_px))
        img = ImageOps.exif_transpose(img)
        if img.mode == "P":                    # paleta não redimensiona bem
            img = img.convert("RGBA")
        img.thumbnail((max_px, max_px), Image.LANCZOS)      # reduz ANTES de converter (menos pixels para copiar)
        if img.mode in ("RGBA", "LA"):
            fundo = Image.new("RGB", img.size, (255, 255, 255))
            img = img.convert("RGBA")
            fundo.paste(img, mask=img.split()[-1])
            img = fundo
        else:
            img = img.convert("RGB")
        fid = uuid.uuid4().hex
        caminho = os.path.join(storage.orig, fid + ".jpg")
        img.save(caminho, "JPEG", quality=qualidade, optimize=True, progressive=True)
        execute("INSERT INTO arquivos (id, nome, mime, tamanho, largura, altura, pasta) VALUES (%s,%s,'image/jpeg',%s,%s,%s,%s)",
                (fid, sanitize_input(nome)[:120], os.path.getsize(caminho), img.width, img.height, pasta))
        return fid

    @staticmethod
    def deletar_imagem(fid):
        if not fid or not re.fullmatch(r"[a-f0-9]{32}", fid):
            return
        for p in [os.path.join(storage.orig, fid + ".jpg")] + glob.glob(os.path.join(storage.cache, fid + "_*")):
            try:
                os.remove(p)
            except OSError:
                pass
        execute("DELETE FROM arquivos WHERE id = %s", (fid,))

    @staticmethod
    def variante(fid, largura, webp=False):
        origem = os.path.join(storage.orig, fid + ".jpg")
        if not os.path.exists(origem):
            return None
        if not largura:
            return origem, "image/jpeg"
        ext = "webp" if webp else "jpg"
        destino = os.path.join(storage.cache, f"{fid}_{largura}.{ext}")
        if not os.path.exists(destino):
            with Image.open(origem) as im:                      # só o cabeçalho é lido aqui
                if not webp and im.width <= largura:
                    return origem, "image/jpeg"                 # já cabe: serve o original (não cria cópia igual no cache)
                with _IMG_SEM:
                    if im.format == "JPEG":
                        im.draft("RGB", (largura, largura))
                    if im.width > largura:
                        im = im.resize((largura, round(im.height * largura / im.width)), Image.LANCZOS)
                    tmp = destino + f".{uuid.uuid4().hex[:6]}.tmp"
                    im.save(tmp, "WEBP" if webp else "JPEG", quality=80, **({} if webp else {"optimize": True, "progressive": True}))
                    os.replace(tmp, destino)
        return destino, "image/webp" if webp else "image/jpeg"


def url_foto(fid, largura=None):
    if not fid:
        return url_for("placeholder")
    if largura:
        largura = min((w for w in LARGURAS if w >= largura), default=LARGURAS[-1])
    return url_for("assets", file_id=fid, w=largura) if largura else url_for("assets", file_id=fid)


# ── Uso de armazenamento (painel admin /admin/arquivos) ──
def fmt_bytes(n):
    """Tamanho legível em português (base 1024): 0 B · 12 KB · 320 MB · 1,4 GB. Tolera None/negativos."""
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        n = 0.0
    if n <= 0:
        return "0 B"
    if n < 1024:
        return f"{int(n)} B"
    for un in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024 or un == "TB":
            break
    if un in ("KB", "MB") and n >= 10:
        txt = f"{n:.0f}"
    else:
        txt = f"{n:.1f}"
        if txt.endswith(".0") and un in ("KB", "MB"):
            txt = txt[:-2]
    return f"{txt.replace('.', ',')} {un}"


@app.template_filter("bytes_h")
def f_bytes_h(v):
    return fmt_bytes(v)


_CACHE_BYTES = {"t": 0.0, "v": 0}


def cache_bytes_cached(ttl=600):
    """Soma o tamanho dos arquivos de storage.cache. Resultado fica 10 min em memória (não varre a pasta a cada acesso)."""
    agora = time.time()
    if _CACHE_BYTES["t"] and agora - _CACHE_BYTES["t"] < ttl:
        return _CACHE_BYTES["v"]
    total = 0
    try:
        with os.scandir(storage.cache) as it:
            for e in it:
                try:
                    if e.is_file(follow_symlinks=False):
                        total += e.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        total = 0
    _CACHE_BYTES["t"], _CACHE_BYTES["v"] = agora, total
    return total


def disco_info():
    try:
        d = shutil.disk_usage(UPLOAD_DIR)
    except OSError:
        return None
    pct = round(d.used / d.total * 100, 1) if d.total else 0.0
    return {"total": d.total, "usado": d.used, "livre": d.free, "pct_usado": pct}


def uso_anunciantes():
    """Uso por anunciante numa única consulta agregada. Ordenado por espaço ocupado (maior primeiro)."""
    linhas = query_all(
        """SELECT t.id, t.nome, t.slug, t.plano, t.assinatura_status, t.plano_vencimento,
                  COUNT(DISTINCT i.id) FILTER (WHERE i.status <> 'rejeitado') AS n_imoveis,
                  COUNT(f.id) FILTER (WHERE i.status <> 'rejeitado') AS n_fotos,
                  COALESCE(SUM(a.tamanho), 0) AS bytes_fotos,
                  COALESCE(MAX(la.tamanho), 0) AS bytes_logo
             FROM tenants t
             LEFT JOIN imoveis i ON i.tenant_id = t.id
             LEFT JOIN imovel_fotos f ON f.imovel_id = i.id
             LEFT JOIN arquivos a ON a.id = f.file_id
             LEFT JOIN arquivos la ON la.id = t.logo_file_id
            GROUP BY t.id""")
    saida = []
    for r in linhas:
        plano = plano_efetivo(r)
        lim = LIMITES[plano]
        n_im, n_fo = int(r["n_imoveis"] or 0), int(r["n_fotos"] or 0)
        lim_fotos_total = lim["imoveis"] * lim["fotos"]
        saida.append(dict(
            id=r["id"], nome=r["nome"], slug=r["slug"], plano_efetivo=plano,
            n_imoveis=n_im, limite_imoveis=lim["imoveis"], n_fotos=n_fo,
            limite_fotos=lim["fotos"], limite_fotos_total=lim_fotos_total,
            bytes=int(r["bytes_fotos"] or 0) + int(r["bytes_logo"] or 0),
            pct_imoveis=min(100, round(n_im * 100 / lim["imoveis"])) if lim["imoveis"] else 0,
            pct_fotos=min(100, round(n_fo * 100 / lim_fotos_total)) if lim_fotos_total else 0))
    saida.sort(key=lambda x: (-x["bytes"], (x["nome"] or "").lower()))
    return saida


@app.route("/assets/<file_id>")
def assets(file_id):
    if not re.fullmatch(r"[a-f0-9]{32}", file_id):
        abort(404)
    w = request.args.get("w", type=int)
    w = w if w in LARGURAS else None
    webp = "image/webp" in request.headers.get("Accept", "")
    r = storage.variante(file_id, w, webp)
    if not r:
        abort(404)
    resp = send_file(r[0], mimetype=r[1], conditional=True, max_age=31536000)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    resp.headers["Vary"] = "Accept"
    return resp


@app.route("/placeholder.svg")
def placeholder():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300"><rect width="400" height="300" fill="#e8eeea"/>'
           '<path d="M120 170l80-60 80 60v70H120z" fill="#c9d6ce"/><rect x="180" y="190" width="40" height="50" fill="#b3c4b9"/></svg>')
    return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


# ═══════════════════════════════════════════════════════════════
# 3b. IMAGENS DO SITE (logo, fundo da home, favicon, imagem de compartilhamento)
#     Ficam em UPLOAD_DIR/site (no volume). O admin envia em /admin/midia e o site já usa sozinho.
# ═══════════════════════════════════════════════════════════════

SITE_NOME = "OndeImóveis"
SITE_TITULO = "OndeImóveis — Casas e apartamentos para comprar ou alugar"
SITE_DESCRICAO = ("Encontre casas e apartamentos para comprar ou alugar perto de você. "
                  "Fale direto com o anunciante pelo WhatsApp.")
SITE_WHATSAPP = re.sub(r"\D", "", os.environ.get("PORTAL_WHATSAPP", ""))   # ex.: 5511999999999 (opcional)
SITE_DIR = os.path.join(UPLOAD_DIR, "site")
os.makedirs(SITE_DIR, exist_ok=True)
SITE_SLOTS = {
    "logo": {"nome": "Logo", "max": 1200, "svg": True, "escuro": True, "capa": False,
             "dica": "Versão para fundo ESCURO (aparece sobre a foto da home, no rodapé e no admin). PNG transparente ou SVG."},
    "hero": {"nome": "Fundo da home (hero)", "max": 2400, "svg": False, "escuro": False, "capa": True,
             "dica": "Foto horizontal, ideal 2200×1100 px. A esquerda fica escurecida para o texto aparecer."},
    "cta": {"nome": "Fundo da faixa final", "max": 2400, "svg": False, "escuro": False, "capa": True,
            "dica": "Foto horizontal, ideal 2200×700 px. Pode ser a mesma da home."},
    "og": {"nome": "Imagem de compartilhamento", "max": 1200, "svg": False, "escuro": False, "capa": True,
           "dica": "Aparece no preview do WhatsApp, Facebook e Google. Exatamente 1200×630 px, JPG ou PNG."},
    "favicon": {"nome": "Favicon (ícone da aba)", "max": 512, "svg": True, "escuro": False, "capa": False,
                "dica": "PNG quadrado 512×512 px (ou SVG), só o símbolo, sem texto."},
}
SITE_NOME_RE = re.compile(r"^[a-z0-9_-]{1,90}\.(png|jpg|webp|svg)$")
_site_cache = {"t": None, "idx": {}}


def _site_indice():
    """{'logo': ('logo.png', mtime), ...} — recalcula só quando a pasta muda."""
    try:
        t = os.stat(SITE_DIR).st_mtime_ns
    except OSError:
        return {}
    if _site_cache["t"] != t:
        idx = {}
        for f in os.listdir(SITE_DIR):
            if SITE_NOME_RE.match(f):
                idx[f.rsplit(".", 1)[0]] = (f, int(os.path.getmtime(os.path.join(SITE_DIR, f))))
        _site_cache.update(t=t, idx=idx)
    return _site_cache["idx"]


def site_url(chave):
    f = _site_indice().get(chave)
    return url_for("site_media", nome=f[0], v=f[1]) if f else None


def base_url():
    return BASE_URL or request.host_url.rstrip("/")


def canonical_url():
    return base_url() + request.path


def site_salvar_imagem(arquivo, slot=None):
    """Valida e grava em SITE_DIR. slot=None → imagem avulsa. Mantém PNG transparente. Devolve o nome do arquivo."""
    dados = arquivo.read()
    if not dados:
        raise ValueError("arquivo vazio")
    if len(dados) > 15 * 1024 * 1024:
        raise ValueError("arquivo maior que 15 MB")
    cfg = SITE_SLOTS.get(slot, {})
    if b"<svg" in dados[:2048].lower():
        if slot is not None and not cfg.get("svg"):
            raise ValueError("este campo não aceita SVG (use PNG, JPG ou WEBP)")
        if slot is None:
            raise ValueError("SVG só pode ser usado no logo e no favicon")
        txt = dados.decode("utf-8", "ignore")
        if re.search(r"<script|javascript:|\son\w+\s*=|<foreignobject|<iframe|<image", txt, re.I):
            raise ValueError("SVG com conteúdo não permitido (scripts/imagens embutidas)")
        ext, saida = "svg", dados
    else:
        Image.open(io.BytesIO(dados)).verify()
        im = Image.open(io.BytesIO(dados))
        fmt = im.format
        if fmt not in MIME_OK:
            raise ValueError("use PNG, JPG ou WEBP")
        im = ImageOps.exif_transpose(im)
        lim = cfg.get("max", 2400)
        im.thumbnail((lim, lim), Image.LANCZOS)
        buf = io.BytesIO()
        if fmt == "PNG":
            im = im.convert("RGBA" if im.mode in ("P", "LA", "RGBA") else "RGB")
            im.save(buf, "PNG", optimize=True); ext = "png"
        elif fmt == "WEBP":
            im = im.convert("RGBA" if im.mode in ("P", "LA", "RGBA") else "RGB")
            im.save(buf, "WEBP", quality=88); ext = "webp"
        else:
            im.convert("RGB").save(buf, "JPEG", quality=88, optimize=True, progressive=True); ext = "jpg"
        saida = buf.getvalue()
    if slot:
        base = slot
    else:
        orig = os.path.splitext(getattr(arquivo, "filename", "") or "imagem")[0]
        base = f"img-{uuid.uuid4().hex[:8]}-{gerar_slug(orig)[:40]}"
    nome = f"{base}.{ext}"
    destino = os.path.join(SITE_DIR, nome)
    tmp = destino + f".{uuid.uuid4().hex[:6]}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(saida)
    for outra in ("png", "jpg", "webp", "svg"):          # troca de formato (logo.png → logo.svg) não deixa lixo
        p = os.path.join(SITE_DIR, f"{base}.{outra}")
        if outra != ext and os.path.exists(p):
            os.remove(p)
    os.replace(tmp, destino)
    return nome


def site_remover_imagem(nome):
    if SITE_NOME_RE.match(nome or ""):
        try:
            os.remove(os.path.join(SITE_DIR, nome))
        except OSError:
            pass


def volume_info():
    """Diz se UPLOAD_DIR está num volume montado (senão as imagens somem a cada deploy)."""
    gravavel, erro = True, ""
    try:
        teste = os.path.join(SITE_DIR, f".w{uuid.uuid4().hex[:6]}")
        with open(teste, "w") as fh:
            fh.write("ok")
        os.remove(teste)
    except OSError as e:
        gravavel, erro = False, str(e)
    p = os.path.abspath(UPLOAD_DIR)
    while p != os.path.dirname(p):
        if os.path.ismount(p):
            return {"caminho": UPLOAD_DIR, "montado": p, "gravavel": gravavel, "erro": erro}
        p = os.path.dirname(p)
    return {"caminho": UPLOAD_DIR, "montado": None, "gravavel": gravavel, "erro": erro}


@app.route("/site-media/<nome>")
def site_media(nome):
    if not SITE_NOME_RE.match(nome):
        abort(404)
    p = os.path.join(SITE_DIR, nome)
    if not os.path.isfile(p):
        abort(404)
    resp = send_file(p, conditional=True)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable" if request.args.get("v") else "public, max-age=3600"
    if nome.endswith(".svg"):
        resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    return resp


@app.route("/favicon.ico")
def favicon():
    f = _site_indice().get("favicon")
    if f:
        return site_media(f[0])
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#071421"/>'
           '<path d="M14 32 32 16l18 16v18H14z" fill="none" stroke="#39e44b" stroke-width="5" stroke-linejoin="round"/></svg>')
    return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


CAPA_SQL = "(SELECT f.file_id FROM imovel_fotos f WHERE f.imovel_id = i.id ORDER BY f.ordem, f.id LIMIT 1)"


def eh_favorito(imovel_id):
    return bool(g.usuario and query_one("SELECT 1 FROM favoritos WHERE usuario_id = %s AND imovel_id = %s",
                                        (g.usuario["id"], imovel_id)))


def preparar_imoveis(lista, largura=480):
    """Põe a URL da capa e o estado de favorito em cada imóvel."""
    favs = set()
    if g.get("usuario") and lista:
        favs = {r["imovel_id"] for r in query_all("SELECT imovel_id FROM favoritos WHERE usuario_id = %s AND imovel_id = ANY(%s)",
                                                  (g.usuario["id"], [i["id"] for i in lista]))}
    for i in lista:
        i["foto"] = url_foto(i.get("capa"), largura)
        i["favorito"] = i["id"] in favs
    return lista


def plano_efetivo(t):
    if t["plano"] == "profissional" and t.get("assinatura_status") == "ativa" and \
            (t.get("plano_vencimento") is None or t["plano_vencimento"] >= hoje()):
        return "profissional"
    return "gratis"


def enriquecer_tenant(t):
    t["plano_efetivo"] = plano_efetivo(t)
    t["pro"] = t["plano_efetivo"] == "profissional"
    t["limites"] = LIMITES[t["plano_efetivo"]]
    t["logo_url"] = url_foto(t["logo_file_id"], 240) if t.get("logo_file_id") and t["pro"] else None
    cor = t.get("cor_primaria") or ""
    t["cor"] = cor if t["pro"] and re.fullmatch(r"#[0-9a-fA-F]{6}", cor) else None
    return t


def _alias_sql(a):
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", a):
        raise ValueError("alias SQL inválido")
    return a


def sql_pro_ativo(alias="t"):
    """Fragmento SQL que espelha plano_efetivo() == 'profissional'. A data vem de hoje() (já com fuso), não do CURRENT_DATE."""
    a = _alias_sql(alias)
    return (f"({a}.plano = 'profissional' AND {a}.assinatura_status = 'ativa' "
            f"AND ({a}.plano_vencimento IS NULL OR {a}.plano_vencimento >= '{hoje().isoformat()}'::date))")


def sql_destaque(i="i", t="t"):
    """Destaque efetivo: o do admin (sempre vale) OU o marcado pelo anunciante enquanto o plano Pro estiver ativo."""
    return f"({_alias_sql(i)}.destaque OR ({_alias_sql(i)}.destaque_anunciante AND {sql_pro_ativo(t)}))"


# ── Templates da página pública (/imobiliarias/<slug>) ────────────────────────
# A lista de templates vem da PASTA templates/paginas/pagina_*.html — nunca do código.
# Para adicionar um template novo basta copiar o arquivo e fazer deploy (sem editar o app.py).
TEMPLATE_PADRAO = "pagina_padrao"
_PASTA_PAGINAS = os.path.join(BASE_DIR, "templates", "paginas")
_META_RE = re.compile(r"^\s*\{#-?\s*(.*?)\s*-?#\}")


def _ler_meta_template(caminho):
    """Lê SÓ a primeira linha: {# nome: Institucional | descricao: Visual escuro #} (opcional)."""
    try:
        with open(caminho, encoding="utf-8") as fh:
            m = _META_RE.match(fh.readline())
    except OSError:
        return {}
    meta = {}
    if m:
        for parte in m.group(1).split("|"):
            chave, sep, valor = parte.partition(":")
            if sep and chave.strip().lower() in ("nome", "descricao"):
                meta[chave.strip().lower()] = valor.strip()
    return meta


@lru_cache(maxsize=1)
def _catalogo_templates_pagina():
    """Varre a pasta uma vez por processo (os arquivos só mudam em deploy)."""
    itens = []
    for caminho in sorted(glob.glob(os.path.join(_PASTA_PAGINAS, "pagina_*.html"))):
        tid = os.path.basename(caminho)[:-len(".html")]
        meta = _ler_meta_template(caminho)
        nome = meta.get("nome") or tid[len("pagina_"):].replace("_", " ").replace("-", " ").capitalize()
        thumb = os.path.exists(os.path.join(BASE_DIR, "static", "templates", tid + ".jpg"))
        itens.append((tid, nome[:60], (meta.get("descricao") or "")[:140], thumb))
    return tuple(itens)


def listar_templates_pagina():
    """Nomes (sem .html) dos templates disponíveis, ordenados. É a lista de PERMITIDOS."""
    return [i[0] for i in _catalogo_templates_pagina()]


def metadados_templates_pagina():
    """[{id, nome, descricao, miniatura}] para os cards do painel."""
    return [dict(id=tid, nome=nome, descricao=desc,
                 miniatura=url_for("static", filename="templates/%s.jpg" % tid) if thumb else None)
            for tid, nome, desc, thumb in _catalogo_templates_pagina()]


def template_do_tenant(t):
    """Caminho do template a renderizar. O valor do banco veio de formulário: só é usado se for Pro
    E estiver EXATAMENTE na lista da pasta; nunca vai direto para render_template."""
    escolhido = t.get("template")
    if t.get("plano_efetivo") == "profissional" and escolhido and escolhido in listar_templates_pagina():
        return "paginas/%s.html" % escolhido
    return "paginas/%s.html" % TEMPLATE_PADRAO


# ═══════════════════════════════════════════════════════════════
# 3c. SESSÃO, CSRF, DECORATORS, CONTEXTO
# ═══════════════════════════════════════════════════════════════

@app.before_request
def _dominio_canonico():
    """O endereço oficial é o do BASE_URL (www.ondeimoveis.com.br): ondeimoveis.com.br, sem o www, vira 301 para ele."""
    if not BASE_URL or request.method not in ("GET", "HEAD") or request.path == "/healthz":
        return None
    oficial = urlparse(BASE_URL).netloc.lower()
    host = request.host.lower()
    if host != oficial and host.replace("www.", "", 1) == oficial.replace("www.", "", 1):
        return redirect(BASE_URL + request.full_path.rstrip("?"), code=301)
    return None


@app.before_request
def _carregar_usuario():
    g.usuario = g.tenant = None
    uid = session.get("uid")
    if uid and DATABASE_URL and not request.path.startswith(("/assets/", "/static/", "/site-media/")):
        u = query_one("SELECT * FROM usuarios WHERE id = %s", (uid,))
        if u:
            g.usuario = u
            if u["tipo"] in TIPOS_TENANT:
                t = query_one("SELECT * FROM tenants WHERE usuario_id = %s", (u["id"],))
                g.tenant = enriquecer_tenant(t) if t else None
        else:
            session.clear()


def _token_csrf():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(24)
    return session["_csrf"]


@app.before_request
def _checar_csrf():
    if request.method in ("POST", "PUT", "DELETE", "PATCH") and not request.path.startswith(("/webhooks/", "/tarefas/")):
        enviado = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
        if not enviado or not hmac.compare_digest(enviado, session.get("_csrf", "")):
            abort(400, "Sessão expirada. Volte e tente de novo.")


def _site_ctx():
    base = base_url()
    og = site_url("og") or site_url("hero")
    return dict(nome=SITE_NOME, base=base, whatsapp=SITE_WHATSAPP, titulo=SITE_TITULO, descricao=SITE_DESCRICAO,
                logo=site_url("logo"), hero=site_url("hero"), cta=site_url("cta"), favicon=site_url("favicon"),
                og=(base + og) if og else None,
                logo_abs=(base + site_url("logo")) if site_url("logo") else None)


@app.context_processor
def _contexto():
    ctx = dict(usuario=g.get("usuario"), TIPOS_IMOVEL=TIPOS_IMOVEL, FINALIDADES=FINALIDADES, STATUS_IMOVEL=STATUS_IMOVEL,
               CATEGORIAS_PROXIMO=CATEGORIAS_PROXIMO, ICONES_PROXIMO=ICONES_PROXIMO, CARACTERISTICAS=CARACTERISTICAS,
               PLANOS=PLANOS, PRECO_PROFISSIONAL=preco_mensal(), wa_link=wa_link, url_foto=url_foto,
               SITE=_site_ctx(), canonical_url=canonical_url, tem_rota=lambda n: n in app.view_functions,
               ano=datetime.now(TZ).year, email_ok=email_ok(g.get("usuario")),
               csrf=lambda: Markup(f'<input type="hidden" name="_csrf" value="{_token_csrf()}">'))
    if g.get("usuario") and g.usuario["tipo"] == "admin" and request.endpoint and request.endpoint.startswith("admin_"):
        n = query_one("SELECT (SELECT COUNT(*) FROM imoveis WHERE status = 'pendente') + "
                      "(SELECT COUNT(*) FROM imovel_proximos WHERE status = 'pendente') AS n")
        ctx["pend_total"] = n["n"]
    return ctx


@app.after_request
def _cabecalhos(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return resp


# ═══════════════════════════════════════════════════════════════
# AVISO DE COOKIES (LGPD) — aparece até a pessoa escolher; a escolha fica 1 ano no cookie `io_cookies`
# ═══════════════════════════════════════════════════════════════

_COOKIE_HTML = """
<div id="io-ck" role="dialog" aria-live="polite" aria-label="Aviso de cookies" style="position:fixed;left:16px;bottom:16px;z-index:99990;width:min(420px,calc(100% - 32px));background:#071421;color:#fff;border-radius:14px;padding:16px 18px;box-shadow:0 14px 40px #0006;font:13px/1.5 Inter,Arial,sans-serif">
<b style="display:block;font-size:14px;margin-bottom:4px">🍪 Cookies</b>
<span style="color:#cfd9de">Usamos cookies essenciais para manter você conectado e, com a sua permissão, para entender como o site é usado e melhorar a experiência.__LINK__</span>
<div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
<button type="button" data-v="todos" style="flex:1;min-width:130px;border:0;border-radius:22px;padding:10px 14px;background:#39dc4b;color:#06150c;font:800 13px Inter,Arial,sans-serif;cursor:pointer">Aceitar todos</button>
<button type="button" data-v="essenciais" style="flex:1;min-width:130px;border:1.5px solid #ffffff55;border-radius:22px;padding:10px 14px;background:none;color:#fff;font:700 13px Inter,Arial,sans-serif;cursor:pointer">Só essenciais</button></div></div>
<script>(function(){var b=document.getElementById('io-ck');if(!b)return;b.querySelectorAll('button').forEach(function(x){x.onclick=function(){document.cookie='io_cookies='+x.dataset.v+';max-age=31536000;path=/;SameSite=Lax'+(location.protocol==='https:'?';Secure':'');b.remove()}})})();</script>
"""


@app.after_request
def _aviso_cookies(resp):
    try:
        if request.method == "GET" and resp.status_code == 200 and resp.mimetype == "text/html":
            resp.vary.add("Cookie")          # o HTML muda conforme a pessoa já escolheu ou não
        if (request.method != "GET" or resp.status_code != 200 or resp.mimetype != "text/html" or resp.direct_passthrough
                or request.cookies.get("io_cookies") or request.path.startswith(("/admin", "/painel", "/chat/", "/api/"))):
            return resp
        corpo = resp.get_data(as_text=True)
        if "</body>" not in corpo:
            return resp
        link = ""
        if "privacidade" in app.view_functions:
            link = f' <a href="{url_for("privacidade")}" style="color:#39dc4b">Política de privacidade</a>.'
        corpo = corpo.replace("</body>", _COOKIE_HTML.replace("__LINK__", link) + "</body>", 1)
        resp.set_data(corpo)
    except Exception:
        log.exception("Aviso de cookies: não consegui inserir o banner")
    return resp


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not g.usuario:
            flash("Entre na sua conta para continuar.", "error")
            return redirect(url_for("entrar", next=request.full_path.rstrip("?") if request.method == "GET" else request.referrer and urlparse(request.referrer).path))
        return f(*a, **k)
    return w


def anunciante_required(f):
    @wraps(f)
    @login_required
    def w(*a, **k):
        if not g.tenant:
            flash("Esta área é para anunciantes. Crie uma conta de anunciante para continuar.", "error")
            return redirect(url_for("painel"))
        if g.tenant["status"] != "ativo":
            flash("Sua conta de anunciante está bloqueada. Fale com o suporte.", "error")
            return redirect(url_for("home"))
        return f(*a, **k)
    return w


def admin_required(f):
    @wraps(f)
    @login_required
    def w(*a, **k):
        if g.usuario["tipo"] != "admin":
            abort(404)
        return f(*a, **k)
    return w


def _smtp_enviar(msg):
    """Envia pela porta configurada (587 + STARTTLS). Se a conexão cair, tenta de novo pela 465 (SSL direto)."""
    ctx = ssl.create_default_context()
    rotas = [(SMTP_PORT, SMTP_PORT == 465)]
    if SMTP_PORT != 465:
        rotas.append((465, True))
    ultimo = None
    for porta, ssl_direto in rotas:
        try:
            srv = (smtplib.SMTP_SSL(SMTP_HOST, porta, timeout=15, context=ctx) if ssl_direto
                   else smtplib.SMTP(SMTP_HOST, porta, timeout=15))
            with srv:
                if not ssl_direto:
                    srv.starttls(context=ctx)
                srv.login(SMTP_USER, SMTP_SENHA)
                srv.send_message(msg)
            return
        except smtplib.SMTPAuthenticationError:
            raise  # senha/usuário recusados: tentar outra porta não adianta
        except (smtplib.SMTPException, OSError) as e:
            log.warning("E-mail de aviso: falhou pela porta %s (%s: %s)", porta, type(e).__name__, e)
            ultimo = e
    raise ultimo


def _enviar_email(assunto, corpo, para=None, html=None):
    """Envia e-mail em segundo plano (nunca trava nem derruba a requisição). Sem `para`, vai para EMAIL_AVISOS (admin)."""
    destino = para or EMAIL_AVISOS
    if not (SMTP_USER and SMTP_SENHA and destino):
        return

    def _job():
        try:
            msg = EmailMessage()
            msg["Subject"] = " ".join(assunto.split())[:150]
            msg["From"] = f"{SITE_NOME} <{SMTP_USER}>"       # o Zoho exige que o remetente seja a própria conta
            msg["To"] = destino
            msg.set_content(corpo)
            if html:
                msg.add_alternative(html, subtype="html")
            _smtp_enviar(msg)
        except smtplib.SMTPAuthenticationError as e:
            log.error("E-mail: o servidor recusou o login de %s (%s %s). Confira SMTP_USER (e-mail completo) e a senha de aplicativo do Zoho.",
                      SMTP_USER, e.smtp_code, e.smtp_error)
        except Exception:
            log.exception("Falha ao enviar e-mail (%s como %s)", SMTP_HOST, SMTP_USER)

    threading.Thread(target=_job, daemon=True).start()


def avisar_novo_anuncio(imovel_id, editado=False):
    """Avisa o admin que há um anúncio esperando aprovação."""
    i = query_one("SELECT i.titulo, i.cidade, i.bairro, t.nome AS anunciante FROM imoveis i "
                  "JOIN tenants t ON t.id = i.tenant_id WHERE i.id = %s", (imovel_id,))
    if not i:
        return
    link = (BASE_URL or request.url_root.rstrip("/")) + "/admin/aprovacoes"
    quando = "editado e voltou para análise" if editado else "novo anúncio aguardando aprovação"
    _enviar_email(
        f"[{SITE_NOME}] Anúncio {quando}: {i['titulo']}",
        f"Anúncio {quando}.\n\nTítulo: {i['titulo']}\nAnunciante: {i['anunciante']}\n"
        f"Local: {i['bairro']}, {i['cidade']}\n\nRevisar agora: {link}\n")


def criar_usuario(nome, email, senha, tipo="visitante", telefone=None, cidade=None, consentimento=False):
    """Cria o usuário (e o 'tenant' se for anunciante). Devolve o id do usuário."""
    nome = sanitize_input(nome)[:120]
    uid = execute_returning(
        "INSERT INTO usuarios (nome, email, senha_hash, tipo, telefone, cidade, consentimento_em, consentimento_versao) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (nome, email.lower().strip(), generate_password_hash(senha), tipo, so_digitos(telefone) or None,
         sanitize_input(cidade or "") or None, agora() if consentimento else None, CONSENTIMENTO_VERSAO if consentimento else None))
    if tipo in TIPOS_TENANT:
        execute("INSERT INTO tenants (usuario_id, nome, slug, tipo, telefone, whatsapp, cidade) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (uid, nome, slug_unico("tenants", gerar_slug(nome)), tipo, so_digitos(telefone) or None,
                 so_digitos(telefone) or None, sanitize_input(cidade or "") or None))
    return uid


_tentativas = {}


def _limite_login():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
    n, t0 = _tentativas.get(ip, (0, time.time()))
    if time.time() - t0 > 600:
        n, t0 = 0, time.time()
    _tentativas[ip] = (n + 1, t0)
    if len(_tentativas) > 5000:
        _tentativas.clear()
    return n >= 10


def email_ok(u):
    """Pode ver contatos? Admin sempre; os demais, só com e-mail confirmado (se a verificação estiver ligada)."""
    return bool(u) and (u["tipo"] == "admin" or not VERIFICAR_EMAIL or bool(u.get("email_verificado_em")))


def verificado_required(f):
    @wraps(f)
    @login_required
    def w(*a, **k):
        if not email_ok(g.usuario):
            flash("Confirme seu e-mail para ver o contato.", "error")
            volta = request.full_path.rstrip("?") if request.method == "GET" else (request.referrer and urlparse(request.referrer).path)
            return redirect(url_for("verificar", next=volta if seguro_next(volta or "") else ""))
        return f(*a, **k)
    return w


def _hash_codigo(uid, codigo):
    return hashlib.sha256(f"{app.secret_key}:{uid}:{codigo}".encode()).hexdigest()


def enviar_codigo_verificacao(u):
    """Gera e envia o código de 6 dígitos. Devolve (ok, erro). Limites: 1 envio por minuto, 5 por hora."""
    ult = query_one("SELECT criado_em FROM verificacoes WHERE usuario_id = %s ORDER BY id DESC LIMIT 1", (u["id"],))
    if ult and (agora() - ult["criado_em"]).total_seconds() < 60:
        return False, "Aguarde um minuto para pedir outro código."
    if query_one("SELECT COUNT(*) AS n FROM verificacoes WHERE usuario_id = %s AND criado_em > NOW() - INTERVAL '1 hour'",
                 (u["id"],))["n"] >= 5:
        return False, "Você pediu muitos códigos. Tente de novo daqui a uma hora."
    codigo = f"{secrets.randbelow(10 ** 6):06d}"
    execute("UPDATE verificacoes SET usado = TRUE WHERE usuario_id = %s AND NOT usado", (u["id"],))
    execute("INSERT INTO verificacoes (usuario_id, codigo_hash, expira_em) VALUES (%s,%s,NOW() + INTERVAL '15 minutes')",
            (u["id"], _hash_codigo(u["id"], codigo)))
    nome = (u["nome"] or "").split()[0] if u.get("nome") else ""
    texto = (f"Olá{', ' + nome if nome else ''}!\n\nSeu código de verificação do {SITE_NOME} é: {codigo}\n\n"
             f"Ele vale por 15 minutos. Se não foi você que criou a conta, ignore este e-mail.\n")
    html = (f'<div style="font-family:Arial,sans-serif;max-width:460px;margin:auto;color:#071421">'
            f'<h2 style="margin:0 0 12px">Confirme seu e-mail</h2><p>Olá{", " + nome if nome else ""}! Use este código no {SITE_NOME}:</p>'
            f'<p style="font-size:34px;letter-spacing:8px;font-weight:800;background:#f2fbf3;border-radius:12px;padding:16px;text-align:center;margin:18px 0">{codigo}</p>'
            f'<p style="color:#68757d;font-size:13px">Vale por 15 minutos. Se não foi você que criou a conta, ignore este e-mail.</p></div>')
    _enviar_email(f"{codigo} é o seu código do {SITE_NOME}", texto, para=u["email"], html=html)
    return True, None


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.route("/entrar", methods=["GET", "POST"])
def entrar():
    nxt = request.values.get("next", "")
    nxt = nxt if seguro_next(nxt) else ""
    if g.usuario and request.method == "GET":
        return redirect(nxt or url_for("painel"))
    email = request.form.get("email", "").strip().lower()
    if request.method == "POST":
        if _limite_login():
            flash("Muitas tentativas. Aguarde alguns minutos.", "error")
            return render_template("entrar.html", next=nxt, email=email), 429
        u = query_one("SELECT * FROM usuarios WHERE email = %s", (email,))
        if u and check_password_hash(u["senha_hash"], request.form.get("senha", "")):
            session.clear()
            session["uid"] = u["id"]
            session.permanent = bool(request.form.get("lembrar"))
            if u["tipo"] == "visitante" and not email_ok(u):
                return redirect(url_for("verificar", next=nxt))
            return redirect(nxt or url_for("painel"))
        flash("E-mail ou senha incorretos.", "error")
    return render_template("entrar.html", next=nxt, email=email)


@app.route("/cadastrar", methods=["GET", "POST"])
def cadastrar():
    nxt = request.values.get("next", "")
    nxt = nxt if seguro_next(nxt) else ""
    tipo = request.values.get("tipo", "visitante")
    tipo = tipo if tipo in TIPOS_CONTA else "visitante"
    f = {"tipo": tipo, "nome": request.values.get("nome", "")[:120], "email": request.form.get("email", ""),
         "telefone": request.values.get("telefone", "")[:30]}      # nome/telefone podem vir pré-preenchidos da home
    if request.method == "POST":
        nome, email, senha = sanitize_input(f["nome"]), f["email"].strip().lower(), request.form.get("senha", "")
        erro = None
        if len(nome) < 3:
            erro = "Informe seu nome."
        elif not EMAIL_RE.match(email) or len(email) > 200:
            erro = "Informe um e-mail válido."
        elif len(senha) < 8:
            erro = "A senha precisa ter pelo menos 8 caracteres."
        elif len(so_digitos(f["telefone"])) < 10:
            erro = "Informe um WhatsApp com DDD."
        elif not request.form.get("aceite"):
            erro = "Para criar a conta, aceite os termos e a política de privacidade."
        elif query_one("SELECT 1 FROM usuarios WHERE email = %s", (email,)):
            erro = "Já existe uma conta com esse e-mail. Tente entrar."
        if erro:
            flash(erro, "error")
        else:
            uid = criar_usuario(nome, email, senha, tipo, f["telefone"], consentimento=True)
            session.clear()
            session["uid"] = uid
            flash("Conta criada! Bem-vindo ao OndeImóveis.", "success")
            if tipo in TIPOS_TENANT:
                flash("Complete a página do seu negócio e cadastre o primeiro imóvel.", "success")
            if VERIFICAR_EMAIL:
                enviar_codigo_verificacao({"id": uid, "nome": nome, "email": email})
                return redirect(url_for("verificar", next=nxt))
            return redirect(nxt or url_for("painel"))
    return render_template("cadastro.html", next=nxt, **f)


@app.route("/verificar", methods=["GET", "POST"])
@login_required
def verificar():
    nxt = request.values.get("next", "")
    nxt = nxt if seguro_next(nxt) else ""
    destino = nxt or url_for("painel")
    u = g.usuario
    if email_ok(u):
        return redirect(destino)
    if request.method == "POST":
        cod = so_digitos(request.form.get("codigo", ""))[:6]
        v = query_one("SELECT * FROM verificacoes WHERE usuario_id = %s AND NOT usado ORDER BY id DESC LIMIT 1", (u["id"],))
        erro = None
        if not v or v["expira_em"] < agora():
            erro = "Esse código expirou. Peça um novo."
        elif v["tentativas"] >= 5:
            erro = "Muitas tentativas erradas. Peça um novo código."
        elif len(cod) != 6 or not hmac.compare_digest(v["codigo_hash"], _hash_codigo(u["id"], cod)):
            execute("UPDATE verificacoes SET tentativas = tentativas + 1 WHERE id = %s", (v["id"],))
            erro = "Código incorreto. Confira o e-mail e tente de novo."
        if erro:
            flash(erro, "error")
        else:
            execute("UPDATE verificacoes SET usado = TRUE WHERE id = %s", (v["id"],))
            execute("UPDATE usuarios SET email_verificado_em = NOW() WHERE id = %s", (u["id"],))
            flash("E-mail confirmado! Agora você pode ver os contatos.", "success")
            return redirect(destino)
    else:
        # chegou aqui sem um código válido (ex.: conta antiga ou login em outro aparelho): manda um automaticamente
        ativo = query_one("SELECT 1 FROM verificacoes WHERE usuario_id = %s AND NOT usado AND expira_em > NOW() AND tentativas < 5", (u["id"],))
        if not ativo:
            ok, msg = enviar_codigo_verificacao(u)
            if ok:
                flash("Enviamos um código para o seu e-mail.", "success")
            elif msg:
                flash(msg, "error")
    e = u["email"]
    mascarado = e[0] + "•" * max(1, e.index("@") - 1) + e[e.index("@"):] if "@" in e and e.index("@") > 1 else e
    return render_template("verificar.html", next=nxt, email=mascarado)


@app.route("/verificar/reenviar", methods=["POST"])
@login_required
def verificar_reenviar():
    nxt = request.form.get("next", "")
    nxt = nxt if seguro_next(nxt) else ""
    if not email_ok(g.usuario):
        ok, msg = enviar_codigo_verificacao(g.usuario)
        flash("Enviamos um novo código para o seu e-mail." if ok else msg, "success" if ok else "error")
    return redirect(url_for("verificar", next=nxt))


@app.route("/sair")
def sair():
    session.clear()
    return redirect(url_for("home"))


@app.route("/painel")
@login_required
def painel():
    if g.usuario["tipo"] == "admin":
        return redirect(url_for("admin_dashboard"))
    if g.tenant:
        return redirect(url_for("anunciante_visao_geral"))
    return redirect(url_for("minha_area"))


@app.route("/como-funciona")
def como_funciona():
    return render_template("como_funciona.html")


@app.route("/healthz")
def healthz():
    query_one("SELECT 1")
    return jsonify(ok=True)


@app.errorhandler(404)
def _404(e):
    return render_template("404.html", codigo=404, mensagem="Não encontramos essa página."), 404


@app.errorhandler(400)
def _400(e):
    return render_template("404.html", codigo=400, mensagem=getattr(e, "description", "Requisição inválida.")), 400


@app.errorhandler(413)
def _413(e):
    return render_template("404.html", codigo=413, mensagem="Os arquivos enviados são grandes demais (máx. 60 MB)."), 413


@app.errorhandler(500)
def _500(e):
    log.exception("Erro interno")
    return render_template("404.html", codigo=500, mensagem="Algo deu errado. Tente novamente."), 500



# ═══════════════════════════════════════════════════════════════
# 4. PORTAL PÚBLICO
# ═══════════════════════════════════════════════════════════════

POR_PAGINA = 10
ORDENS = {"relevantes": "{destaque} DESC, i.criado_em DESC", "menor": "i.preco ASC", "maior": "i.preco DESC",
          "recentes": "i.criado_em DESC"}


def ordem_busca_sql(chave):
    """ORDER BY da busca. O destaque efetivo depende da data de hoje, então é montado a cada consulta."""
    return ORDENS[chave].replace("{destaque}", sql_destaque())


def _montar_qs(args):
    """Devolve uma função qs(pagina=2, ordem='menor') que mantém os filtros atuais na URL."""
    base = [(k, v) for k in args for v in args.getlist(k) if k != "pagina" and v != ""]

    def qs(**mudar):
        itens = [(k, v) for k, v in base if k not in mudar]
        itens += [(k, v) for k, v in mudar.items() if v not in (None, "")]
        from urllib.parse import urlencode  # noqa
        return urlencode(itens)
    return qs


def consulta_imoveis(args, extra_where="", extra_params=(), por_pagina=POR_PAGINA):
    """Busca com filtros (usada em /buscar e na página da imobiliária)."""
    where = ["i.status = 'publicado'", "t.status = 'ativo'"]
    params = list(extra_params)
    if extra_where:
        where.append(extra_where)
    f = {"q": sanitize_input(args.get("q", "").strip())[:80], "finalidade": args.get("finalidade", ""),
         "tipo": [x for x in args.getlist("tipo") if x in TIPOS_IMOVEL],
         "preco_min": num_br(args.get("preco_min")), "preco_max": num_br(args.get("preco_max")),
         "quartos": args.get("quartos", type=int), "vagas": args.get("vagas", type=int),
         "bairro": sanitize_input(args.get("bairro", "").strip())[:80],
         "cidade": sanitize_input(args.get("cidade", "").strip())[:80],
         "uf": re.sub(r"[^A-Za-z]", "", args.get("uf", ""))[:2].upper(),
         "mcmv": args.get("mcmv") == "1",
         "ordem": args.get("ordem", "relevantes") if args.get("ordem") in ORDENS else "relevantes"}
    if f["mcmv"]:
        where.append("i.aceita_mcmv")
    if f["finalidade"] in FINALIDADES:
        where.append("i.finalidade = %s"); params.append(f["finalidade"])
    else:
        f["finalidade"] = ""
    if f["tipo"]:
        where.append("i.tipo = ANY(%s)"); params.append(f["tipo"])
    if f["preco_min"] is not None:
        where.append("i.preco >= %s"); params.append(f["preco_min"])
    if f["preco_max"]:
        where.append("i.preco <= %s"); params.append(f["preco_max"])
    if f["quartos"]:
        where.append("i.dormitorios >= %s"); params.append(f["quartos"])
    if f["vagas"]:
        where.append("i.vagas >= %s"); params.append(f["vagas"])
    if f["bairro"]:
        where.append("i.bairro_slug LIKE %s"); params.append(f"%{gerar_slug(f['bairro'])}%")
    if f["uf"]:
        where.append("UPPER(i.uf) = %s"); params.append(f["uf"])
    if f["cidade"]:
        where.append("i.cidade_slug = %s"); params.append(gerar_slug(f["cidade"]))
    if f["q"]:
        parte = f["q"].split(",")[0].strip()
        slug = gerar_slug(parte)
        where.append("(i.bairro_slug LIKE %s OR i.cidade_slug LIKE %s OR i.titulo ILIKE %s)")
        params += [f"%{slug}%", f"%{slug}%", f"%{parte}%"]
    cond = " AND ".join(where)
    juncao = "FROM imoveis i JOIN tenants t ON t.id = i.tenant_id"
    total = query_one(f"SELECT COUNT(*) AS n {juncao} WHERE {cond}", params)["n"]
    paginas = max(1, math.ceil(total / por_pagina))
    pagina = min(max(1, args.get("pagina", 1, type=int)), paginas)
    lista = query_all(
        f"SELECT i.*, t.nome AS anunciante_nome, t.slug AS anunciante_slug, {CAPA_SQL} AS capa {juncao} "
        f"WHERE {cond} ORDER BY {ordem_busca_sql(f['ordem'])}, i.id DESC LIMIT %s OFFSET %s",
        params + [por_pagina, (pagina - 1) * por_pagina])
    return anexar_fotos(preparar_imoveis(lista)), total, pagina, paginas, f


def anexar_fotos(lista, maximo=6):
    """Põe em cada imóvel a lista de fotos (file_id) usada no carrossel do card (até `maximo`)."""
    por_id = {i["id"]: i for i in lista}
    for i in lista:
        i["fotos"] = []
    if por_id:
        for r in query_all("SELECT imovel_id, file_id FROM imovel_fotos WHERE imovel_id = ANY(%s) ORDER BY ordem, id",
                           (list(por_id),)):
            fs = por_id[r["imovel_id"]]["fotos"]
            if len(fs) < maximo:
                fs.append(r["file_id"])
    return lista


def _faixa_paginas(pagina, paginas):
    return [p for p in range(max(1, pagina - 2), min(paginas, pagina + 2) + 1)]


TIPO_PLURAL = {"apartamento": "Apartamentos", "casa": "Casas", "cobertura": "Coberturas",
               "comercial": "Imóveis comerciais", "terreno": "Terrenos"}
FRASE_FINALIDADE = {"venda": "à venda", "aluguel": "para alugar"}


def cidades_home(limite=10):
    """Cidades com mais imóveis publicados e, por finalidade, os links por tipo (só os que têm imóvel)."""
    linhas = query_all(
        "SELECT MIN(i.cidade) AS cidade, MIN(i.uf) AS uf, i.cidade_slug, i.finalidade, i.tipo, COUNT(*) AS n "
        "FROM imoveis i JOIN tenants t ON t.id = i.tenant_id WHERE i.status = 'publicado' AND t.status = 'ativo' "
        "GROUP BY i.cidade_slug, i.finalidade, i.tipo")
    cidades = {}
    for r in linhas:
        c = cidades.setdefault(r["cidade_slug"], {"nome": r["cidade"], "uf": r["uf"], "slug": r["cidade_slug"], "total": 0,
                                                  "venda": [], "aluguel": []})
        c["total"] += r["n"]
        if r["finalidade"] in FRASE_FINALIDADE and r["tipo"] in TIPO_PLURAL:
            c[r["finalidade"]].append({"tipo": r["tipo"], "n": r["n"]})
    ordem_tipos = list(TIPO_PLURAL)
    saida = []
    for c in sorted(cidades.values(), key=lambda c: (-c["total"], c["nome"] or ""))[:limite]:
        for fin in FRASE_FINALIDADE:
            c[fin].sort(key=lambda x: ordem_tipos.index(x["tipo"]))
            c[fin] = [{"rotulo": f"{TIPO_PLURAL[x['tipo']]} {FRASE_FINALIDADE[fin]} em {c['nome']}", "n": x["n"],
                       "url": url_for("buscar", finalidade=fin, tipo=x["tipo"], cidade=c["slug"])} for x in c[fin]]
            c[fin + "_url"] = url_local(c["slug"], fin=fin)
        saida.append(c)
    return saida


@app.route("/")
def home():
    destaques = query_all(
        f"SELECT i.*, {CAPA_SQL} AS capa FROM imoveis i JOIN tenants t ON t.id = i.tenant_id "
        f"WHERE i.status = 'publicado' AND t.status = 'ativo' AND {sql_destaque()} ORDER BY i.criado_em DESC LIMIT 4")
    imobs = [enriquecer_tenant(t) for t in query_all(
        f"SELECT t.* FROM tenants t WHERE t.status = 'ativo' AND t.tipo IN ('imobiliaria', 'corretor') AND {sql_pro_ativo('t')} "
        "AND EXISTS (SELECT 1 FROM imoveis i WHERE i.tenant_id = t.id AND i.status = 'publicado') ORDER BY random() LIMIT 60")]
    total = query_one("SELECT COUNT(*) AS n FROM imoveis WHERE status = 'publicado'")["n"]
    return render_template("home.html", destaques=preparar_imoveis(destaques, 640), imobs=imobs, total=total, cidades=cidades_home())


def rotulo_local(f):
    """Texto do campo 'Cidade ou bairro' quando há um local escolhido (ex.: 'Pinheiros, São Paulo - SP')."""
    if not f["cidade"]:
        return ""  # só UF: o rótulo ('São Paulo (SP)') é montado no navegador
    r = query_one("SELECT MIN(cidade) AS cidade, MIN(uf) AS uf FROM imoveis WHERE cidade_slug = %s",
                  (gerar_slug(f["cidade"]),)) or {}
    if not r.get("cidade"):
        return ""
    cidade = r["cidade"] + (f" - {r['uf'].upper()}" if r.get("uf") else "")
    return f"{f['bairro']}, {cidade}" if f["bairro"] else cidade


def url_local(cidade, bairro=None, fin=None):
    """URL limpa para SEO: /sao-paulo, /sao-paulo/barra-funda, /sao-paulo/barra-funda/venda, /sao-paulo/venda."""
    p = "/" + cidade
    if bairro:
        p += "/" + bairro
    if fin:
        p += "/" + fin
    return p


def _redirecionar_busca_simples():
    """/buscar?cidade=sao-paulo&bairro=barra-funda&finalidade=venda → /sao-paulo/barra-funda/venda (301), só quando
    a busca tem apenas esses filtros e o local existe. Qualquer outro filtro continua em /buscar."""
    a = {k: v.strip() for k, v in request.args.items() if v.strip()}
    if not a.get("cidade") or not set(a) <= {"cidade", "bairro", "finalidade", "ordem"} or a.get("ordem", "relevantes") != "relevantes":
        return None
    if len(request.args.getlist("cidade")) > 1 or (a.get("finalidade") and a["finalidade"] not in FINALIDADES):
        return None
    c, b = gerar_slug(a["cidade"]), (gerar_slug(a["bairro"]) if a.get("bairro") else None)
    if query_one("SELECT 1 FROM imoveis i JOIN tenants t ON t.id = i.tenant_id WHERE i.status = 'publicado' AND t.status = 'ativo' "
                 "AND i.cidade_slug = %s" + (" AND i.bairro_slug = %s" if b else "") + " LIMIT 1", (c, b) if b else (c,)):
        return redirect(url_local(c, b, a.get("finalidade")), 301)
    return None


@app.route("/buscar")
def buscar():
    r = _redirecionar_busca_simples()
    if r:
        return r
    lista, total, pagina, paginas, f = consulta_imoveis(request.args)
    resp = make_response(render_template("resultados.html", imoveis=lista, total=total, pagina=pagina, paginas=paginas,
                                         faixa=_faixa_paginas(pagina, paginas), f=f, qs=_montar_qs(request.args),
                                         loc_label=rotulo_local(f)))
    if request.args:        # combinações de filtros não entram no Google (as páginas de cidade/bairro é que ranqueiam)
        resp.headers["X-Robots-Tag"] = "noindex, follow"
    return resp


# ── Páginas de cidade e bairro (SEO) ──────────────────────────────────────────
FRASE_FIN_SEO = {"venda": "à venda", "aluguel": "para alugar"}


def _brl(v):
    return "R$ " + f"{float(v):,.0f}".replace(",", ".")


def _qtd(n, um, varios):
    return f"{n} {um if n == 1 else varios}"


def _injetar_seo(html, s):
    """Põe title, description, canonical, Open Graph e JSON-LD no <head> e o bloco de texto/links antes do rodapé.
    Assim as páginas de local funcionam mesmo antes de o resultados.html ter marcações próprias."""
    esc = lambda x: str(html_escape(x))
    html = re.sub(r"<title>.*?</title>\s*", "", html, count=1, flags=re.S | re.I)
    html = re.sub(r"<meta[^>]+(?:name=[\"']description[\"']|property=[\"']og:(?:title|description|url)[\"'])[^>]*>\s*", "", html, flags=re.I)
    html = re.sub(r"<link[^>]+rel=[\"']canonical[\"'][^>]*>\s*", "", html, flags=re.I)
    head = (f"<title>{esc(s['titulo'])}</title>\n"
            f'<meta name="description" content="{esc(s["descricao"])}">\n'
            f'<link rel="canonical" href="{esc(s["canonical"])}">\n'
            f'<meta property="og:type" content="website"><meta property="og:title" content="{esc(s["titulo"])}">\n'
            f'<meta property="og:description" content="{esc(s["descricao"])}"><meta property="og:url" content="{esc(s["canonical"])}">\n'
            f'<script type="application/ld+json">{s["jsonld"]}</script>\n')
    if re.search(r"</head>", html, re.I):
        html = re.sub(r"</head>", lambda m: head + "</head>", html, count=1, flags=re.I)
    else:
        html = head + html
    if "data-seo-local" not in html:
        bloco = render_template("_seo_local.html", seo=s)
        if re.search(r"<footer", html, re.I):
            html = re.sub(r"<footer", lambda m: bloco + "<footer", html, count=1, flags=re.I)
        else:
            html = re.sub(r"</body>", lambda m: bloco + "</body>", html, count=1, flags=re.I) if "</body>" in html.lower() else html + bloco
    return html


@app.route("/<cidade>", defaults={"seg2": None, "seg3": None}, strict_slashes=False)
@app.route("/<cidade>/<seg2>", defaults={"seg3": None}, strict_slashes=False)
@app.route("/<cidade>/<seg2>/<seg3>", strict_slashes=False)
def local(cidade, seg2=None, seg3=None):
    """/sao-paulo · /sao-paulo/venda · /sao-paulo/barra-funda · /sao-paulo/barra-funda/aluguel"""
    fin = None
    bairro = None
    if seg2 in FINALIDADES and seg3 is None:
        fin = seg2
    else:
        bairro, fin = seg2, seg3
    if fin is not None and fin not in FINALIDADES:
        abort(404)
    partes = [cidade] + ([bairro] if bairro else [])
    if any(p != gerar_slug(p) for p in partes):          # só slugs "limpos" (minúsculas, sem acento, com hífen)
        if all(p.lower() == gerar_slug(p) for p in partes):
            return redirect(url_local(*[p.lower() for p in partes[:1]], bairro.lower() if bairro else None, fin), 301)
        abort(404)

    base_cond = "i.status = 'publicado' AND t.status = 'ativo' AND i.cidade_slug = %s"
    juncao = "FROM imoveis i JOIN tenants t ON t.id = i.tenant_id"
    p_cid = [cidade]
    cond, params = base_cond, list(p_cid)
    if bairro:
        cond += " AND i.bairro_slug = %s"; params.append(bairro)
    if fin:
        cond += " AND i.finalidade = %s"; params.append(fin)
    st = query_one(f"SELECT COUNT(*) AS n, MIN(i.preco) AS pmin, MAX(i.preco) AS pmax, MIN(i.cidade) AS cidade, MIN(i.uf) AS uf, "
                   f"MIN(i.bairro) AS bairro {juncao} WHERE {cond}", params)
    if not st or not st["n"]:
        abort(404)                                         # local sem imóvel não existe (evita página vazia indexada)
    n = st["n"]
    nome_cidade = st["cidade"]
    uf = (st["uf"] or "").upper()
    nome_bairro = st["bairro"] if bairro else None
    cid_uf = f"{nome_cidade} - {uf}" if uf else nome_cidade
    local_txt = f"{nome_bairro}, {cid_uf}" if bairro else cid_uf

    por_fin = {r["finalidade"]: r["n"] for r in query_all(
        f"SELECT i.finalidade, COUNT(*) AS n {juncao} WHERE {base_cond}" + (" AND i.bairro_slug = %s" if bairro else "") + " GROUP BY i.finalidade",
        p_cid + ([bairro] if bairro else []))}
    tipos = [r["tipo"] for r in query_all(f"SELECT i.tipo, COUNT(*) AS n {juncao} WHERE {cond} GROUP BY i.tipo ORDER BY n DESC", params)
             if r["tipo"] in TIPO_PLURAL]
    bairros = [{"nome": r["nome"], "n": r["n"], "url": url_local(cidade, r["slug"], fin)} for r in query_all(
        f"SELECT i.bairro_slug AS slug, MIN(i.bairro) AS nome, COUNT(*) AS n {juncao} WHERE {base_cond}"
        + (" AND i.finalidade = %s" if fin else "") + " GROUP BY i.bairro_slug ORDER BY n DESC, nome LIMIT 40",
        p_cid + ([fin] if fin else [])) if r["slug"] != bairro]
    fins = [{"rotulo": f"Imóveis {FRASE_FIN_SEO[k]}", "n": por_fin[k], "url": url_local(cidade, bairro, k), "atual": k == fin}
            for k in FINALIDADES if por_fin.get(k)]
    if len(fins) > 1:
        fins.insert(0, {"rotulo": "Todos os imóveis", "n": sum(por_fin.values()), "url": url_local(cidade, bairro), "atual": fin is None})
    else:
        fins = []

    frase = FRASE_FIN_SEO.get(fin, "")
    h1 = f"Imóveis {frase} em {local_txt}".replace("  ", " ")
    titulo = f"{h1} | {SITE_NOME}"
    if len(titulo) > 70:
        titulo = h1[:67] + "…" if len(h1) > 70 else h1
    sufixo_preco = "/mês" if fin == "aluguel" else ""
    intro = [f"Encontre {_qtd(n, 'imóvel', 'imóveis')} {frase} em {local_txt}".replace("  ", " ") + "."]
    if fin and st["pmin"] is not None:
        intro.append(f"Os valores vão de {_brl(st['pmin'])}{sufixo_preco} a {_brl(st['pmax'])}{sufixo_preco}." if st["pmin"] != st["pmax"]
                     else f"O valor é de {_brl(st['pmin'])}{sufixo_preco}.")
    if tipos:
        nomes = [TIPO_PLURAL[t].lower() for t in tipos[:4]]
        intro.append("Entre as opções estão " + (", ".join(nomes[:-1]) + " e " + nomes[-1] if len(nomes) > 1 else nomes[0]) + ".")
    intro.append(f"No {SITE_NOME} você vê fotos e detalhes e fala direto com proprietários, corretores e imobiliárias pelo WhatsApp.")
    texto = " ".join(intro)
    descricao = " ".join(intro[:-1]) + " Veja fotos e fale direto com o anunciante."
    if len(descricao) > 158:
        descricao = " ".join(intro[:-1])
    if len(descricao) > 158:
        descricao = descricao[:155].rsplit(" ", 1)[0] + "…"

    base = base_url()
    migalhas = [("Início", "/"), (nome_cidade, url_local(cidade))]
    if bairro:
        migalhas.append((nome_bairro, url_local(cidade, bairro)))
    if fin:
        migalhas.append((FINALIDADES[fin], url_local(cidade, bairro, fin)))
    jsonld = json.dumps({"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": k, "name": nm, "item": base + u} for k, (nm, u) in enumerate(migalhas, 1)]},
        ensure_ascii=False).replace("</", "<\\/")
    seo = dict(titulo=titulo, descricao=descricao, canonical=base + url_local(cidade, bairro, fin),
               h1=h1, intro=texto, jsonld=jsonld, migalhas=migalhas, bairros=bairros, fins=fins,
               cidade=nome_cidade, bairro=nome_bairro, h2=f"Sobre {nome_bairro or nome_cidade}")

    # a listagem usa a mesma consulta da busca; o bairro é filtrado pelo slug exato (sem o "contém" da busca livre)
    args_busca = MultiDict([(k, v) for k in request.args for v in request.args.getlist(k)
                            if k not in ("cidade", "bairro", "finalidade", "uf", "q")])
    args_busca.add("cidade", cidade)
    if fin:
        args_busca.add("finalidade", fin)
    args_cons = MultiDict(args_busca)
    qs_args = MultiDict(args_busca)
    if bairro:
        qs_args.add("bairro", nome_bairro)                 # paginação/filtros do template seguem para /buscar com o bairro
    lista, total, pagina, paginas, f = consulta_imoveis(
        args_cons, extra_where="i.bairro_slug = %s" if bairro else "", extra_params=(bairro,) if bairro else ())
    if bairro:
        f["bairro"] = nome_bairro
    html = render_template("resultados.html", imoveis=lista, total=total, pagina=pagina, paginas=paginas,
                           faixa=_faixa_paginas(pagina, paginas), f=f, qs=_montar_qs(qs_args),
                           loc_label=rotulo_local(f), seo=seo)
    resp = make_response(_injetar_seo(html, seo))
    if pagina > 1:                                         # páginas 2, 3… aparecem para quem navega, mas só a 1ª ranqueia
        resp.headers["X-Robots-Tag"] = "noindex, follow"
    return resp


@app.route("/api/locais")
def api_locais():
    """Estados, cidades e bairros que têm imóvel publicado (alimenta o campo 'Cidade ou bairro')."""
    where = ["i.status = 'publicado'", "t.status = 'ativo'"]
    params = []
    if request.args.get("finalidade") in FINALIDADES:
        where.append("i.finalidade = %s"); params.append(request.args["finalidade"])
    tipos = [x for x in request.args.getlist("tipo") if x in TIPOS_IMOVEL]
    if tipos:
        where.append("i.tipo = ANY(%s)"); params.append(tipos)
    linhas = query_all(
        "SELECT UPPER(COALESCE(i.uf, '')) AS uf, MIN(i.cidade) AS cidade, i.cidade_slug, MIN(i.bairro) AS bairro, "
        "i.bairro_slug, COUNT(*) AS n FROM imoveis i JOIN tenants t ON t.id = i.tenant_id "
        f"WHERE {' AND '.join(where)} GROUP BY UPPER(COALESCE(i.uf, '')), i.cidade_slug, i.bairro_slug "
        "ORDER BY n DESC LIMIT 5000", params)
    resp = jsonify([[r["uf"], r["cidade"], r["cidade_slug"], r["bairro"], r["bairro_slug"], r["n"]] for r in linhas])
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


@app.route("/imovel/<slug>")
def imovel(slug):
    i = query_one("SELECT i.*, t.nome AS anunciante_nome, t.slug AS anunciante_slug, t.creci AS anunciante_creci, "
                  "t.whatsapp AS anunciante_whatsapp, t.logo_file_id AS anunciante_logo, t.status AS tenant_status, "
                  "t.verificada AS anunciante_verificada "
                  "FROM imoveis i JOIN tenants t ON t.id = i.tenant_id WHERE i.slug = %s", (slug,))
    if not i:
        abort(404)
    dono = g.tenant and g.tenant["id"] == i["tenant_id"]
    admin = g.usuario and g.usuario["tipo"] == "admin"
    if (i["status"] != "publicado" or i["tenant_status"] != "ativo") and not (dono or admin):
        abort(404)
    fotos = [url_foto(r["file_id"], 1200) for r in
             query_all("SELECT file_id FROM imovel_fotos WHERE imovel_id = %s ORDER BY ordem, id", (i["id"],))]
    proximos = query_all("SELECT * FROM imovel_proximos WHERE imovel_id = %s AND status = 'aprovado' "
                         "ORDER BY categoria, distancia_m NULLS LAST", (i["id"],))
    semelhantes = query_all(
        f"SELECT i2.*, {CAPA_SQL.replace('i.id', 'i2.id')} AS capa FROM imoveis i2 JOIN tenants t ON t.id = i2.tenant_id "
        "WHERE i2.status = 'publicado' AND t.status = 'ativo' AND i2.id <> %s AND i2.finalidade = %s "
        "AND (i2.cidade_slug = %s) ORDER BY (i2.tipo = %s) DESC, ABS(i2.preco - %s) ASC LIMIT 4",
        (i["id"], i["finalidade"], i["cidade_slug"], i["tipo"], i["preco"]))
    for s in semelhantes:
        s["foto"] = url_foto(s["capa"], 480)
    i["favorito"] = eh_favorito(i["id"])
    # conta a visualização (1x por 30 min por visitante, e nunca a do próprio dono)
    if i["status"] == "publicado" and not dono and not admin:
        vistos = session.get("_vistos", {})
        if time.time() - vistos.get(slug, 0) > 1800:
            execute("INSERT INTO eventos (tenant_id, imovel_id, usuario_id, tipo) VALUES (%s,%s,%s,'view')",
                    (i["tenant_id"], i["id"], g.usuario["id"] if g.usuario else None))
            vistos[slug] = time.time()
            session["_vistos"] = dict(list(vistos.items())[-50:])
    numero = i["whatsapp"] or i["anunciante_whatsapp"]
    mapa = None
    if i["lat"] is not None and i["lng"] is not None:
        d = 0.004
        mapa = (f"https://www.openstreetmap.org/export/embed.html?bbox={i['lng'] - d},{i['lat'] - d},"
                f"{i['lng'] + d},{i['lat'] + d}&layer=mapnik&marker={i['lat']},{i['lng']}")
    return render_template("imovel.html", i=i, fotos=fotos, proximos=proximos, semelhantes=semelhantes,
                           carac=[c for c in (i["caracteristicas"] or "").split("|") if c],
                           tem_whatsapp=bool(so_digitos(numero)), mapa=mapa, preview=(dono or admin) and i["status"] != "publicado",
                           logo=url_foto(i["anunciante_logo"], 120) if i["anunciante_logo"] else None)


def _contato_imovel(slug):
    i = query_one("SELECT i.*, t.whatsapp AS t_whatsapp, t.status AS t_status FROM imoveis i JOIN tenants t "
                  "ON t.id = i.tenant_id WHERE i.slug = %s AND i.status = 'publicado'", (slug,))
    if not i or i["t_status"] != "ativo":
        abort(404)
    return i, (i["whatsapp"] or i["t_whatsapp"])


def registrar_lead(origem, canal, tenant_id, imovel=None, detalhe=None):
    """Grava o lead (quem pediu contato). Ignora o dono do anúncio e o admin; não repete o mesmo pedido em 24h."""
    u = g.get("usuario")
    if not u or u["tipo"] == "admin" or (g.get("tenant") and g.tenant["id"] == tenant_id):
        return
    imovel_id = imovel["id"] if imovel else None
    if query_one("SELECT 1 FROM leads WHERE usuario_id = %s AND canal = %s AND tenant_id = %s "
                 "AND imovel_id IS NOT DISTINCT FROM %s AND criado_em > NOW() - INTERVAL '24 hours'",
                 (u["id"], canal, tenant_id, imovel_id)):
        return
    t = query_one("SELECT nome FROM tenants WHERE id = %s", (tenant_id,))
    ip = hashlib.sha256((app.secret_key + (request.remote_addr or "")).encode()).hexdigest()[:24]
    execute(
        "INSERT INTO leads (usuario_id, tenant_id, imovel_id, origem, canal, nome, email, telefone, cidade, tenant_nome, "
        "imovel_titulo, finalidade, tipo_imovel, preco, cidade_imovel, bairro_imovel, quartos, detalhe, cookies, ip_hash, user_agent, "
        "email_verificado, telefone_verificado, consentimento_em) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (u["id"], tenant_id, imovel_id, origem, canal, u["nome"], u["email"], u["telefone"], u["cidade"],
         t["nome"] if t else None,
         imovel["titulo"] if imovel else None, imovel["finalidade"] if imovel else None,
         imovel["tipo"] if imovel else None, imovel["preco"] if imovel else None,
         imovel["cidade"] if imovel else None, imovel["bairro"] if imovel else None,
         imovel["dormitorios"] if imovel else None, detalhe,
         request.cookies.get("io_cookies"), ip, (request.user_agent.string or "")[:300],
         bool(u.get("email_verificado_em")), bool(u.get("telefone_verificado_em")), u.get("consentimento_em")))


@app.route("/imovel/<slug>/whatsapp")
@verificado_required
def imovel_whatsapp(slug):
    i, numero = _contato_imovel(slug)
    if not so_digitos(numero):
        flash("Este anunciante ainda não informou um WhatsApp.", "error")
        return redirect(url_for("imovel", slug=slug))
    execute("INSERT INTO eventos (tenant_id, imovel_id, usuario_id, tipo) VALUES (%s,%s,%s,'whatsapp')",
            (i["tenant_id"], i["id"], g.usuario["id"]))
    registrar_lead("imovel", "whatsapp", i["tenant_id"], i)
    link = (BASE_URL or request.host_url.rstrip("/")) + url_for("imovel", slug=slug)
    texto = f"Olá! Vi o imóvel “{i['titulo']}” no OndeImóveis e gostaria de mais informações. {link}"
    return redirect(wa_link(numero, texto))


@app.route("/imovel/<slug>/visita", methods=["POST"])
@verificado_required
def imovel_visita(slug):
    i, numero = _contato_imovel(slug)
    quando = sanitize_input(request.form.get("quando", "").strip())[:40]
    msg = sanitize_input(request.form.get("mensagem", "").strip())[:300]
    detalhe = " · ".join(x for x in (quando, msg) if x) or None
    execute("INSERT INTO eventos (tenant_id, imovel_id, usuario_id, tipo, detalhe) VALUES (%s,%s,%s,'visita',%s)",
            (i["tenant_id"], i["id"], g.usuario["id"], detalhe))
    registrar_lead("imovel", "visita", i["tenant_id"], i, detalhe)
    flash("Pedido de visita registrado! O anunciante vai ver nos contatos dele.", "success")
    if so_digitos(numero):
        texto = f"Olá! Gostaria de agendar uma visita ao imóvel “{i['titulo']}”" + (f" em {quando}" if quando else "") + "."
        return redirect(wa_link(numero, texto))
    return redirect(url_for("imovel", slug=slug))


@app.route("/favoritos/<int:imovel_id>/toggle", methods=["POST"])
@login_required
def favorito_toggle(imovel_id):
    if not query_one("SELECT 1 FROM imoveis WHERE id = %s AND status = 'publicado'", (imovel_id,)):
        abort(404)
    if execute("DELETE FROM favoritos WHERE usuario_id = %s AND imovel_id = %s", (g.usuario["id"], imovel_id)):
        ativo = False
    else:
        execute("INSERT INTO favoritos (usuario_id, imovel_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (g.usuario["id"], imovel_id))
        ativo = True
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(favorito=ativo)
    ref = urlparse(request.referrer or "")
    voltar = seguro_next(ref.path + ("?" + ref.query if ref.query else "")) if ref.netloc == request.host else None
    return redirect(voltar or url_for("favoritos"))


@app.route("/favoritos")
@login_required
def favoritos():
    ordem = request.args.get("ordem", "recentes")
    ordem_sql = {"recentes": "f.criado_em DESC", "menor": "i.preco ASC", "maior": "i.preco DESC"}.get(ordem, "f.criado_em DESC")
    lista = query_all(
        f"SELECT i.*, t.nome AS anunciante_nome, {CAPA_SQL} AS capa FROM favoritos f JOIN imoveis i ON i.id = f.imovel_id "
        f"JOIN tenants t ON t.id = i.tenant_id WHERE f.usuario_id = %s AND i.status = 'publicado' ORDER BY {ordem_sql}",
        (g.usuario["id"],))
    return render_template("favoritos.html", imoveis=preparar_imoveis(lista, 640), ordem=ordem)


@app.route("/imobiliarias")
def imobiliarias():
    q = sanitize_input(request.args.get("q", "").strip())[:60]
    where, params = "t.status = 'ativo'", []
    if q:
        where += " AND (t.nome ILIKE %s OR t.cidade ILIKE %s)"
        params += [f"%{q}%", f"%{q}%"]
    lista = query_all(
        f"SELECT t.*, (SELECT COUNT(*) FROM imoveis i WHERE i.tenant_id = t.id AND i.status = 'publicado') AS n_imoveis "
        f"FROM tenants t WHERE {where} AND EXISTS (SELECT 1 FROM imoveis i WHERE i.tenant_id = t.id AND i.status = 'publicado') "
        f"ORDER BY {sql_pro_ativo('t')} DESC, t.verificada DESC, n_imoveis DESC, t.nome LIMIT 60", params)
    for t in lista:
        enriquecer_tenant(t)
    # anunciantes Pro: foto grande + miniaturas dos anúncios mais recentes/destacados
    pros = [t for t in lista if t["pro"]]
    galerias, fins = {t["id"]: [] for t in pros}, {t["id"]: [] for t in pros}
    if pros:
        for r in query_all(
                f"SELECT i.tenant_id, i.slug, i.titulo, i.finalidade, {CAPA_SQL} AS capa FROM imoveis i "
                f"JOIN tenants t ON t.id = i.tenant_id "
                f"WHERE i.tenant_id = ANY(%s) AND i.status = 'publicado' ORDER BY {sql_destaque()} DESC, i.criado_em DESC",
                ([t["id"] for t in pros],)):
            if r["finalidade"] in FINALIDADES and r["finalidade"] not in fins[r["tenant_id"]]:
                fins[r["tenant_id"]].append(r["finalidade"])
            if r["capa"] and len(galerias[r["tenant_id"]]) < 4:
                galerias[r["tenant_id"]].append({"big": url_foto(r["capa"], 960), "thumb": url_foto(r["capa"], 320),
                                                 "url": url_for("imovel", slug=r["slug"]), "titulo": r["titulo"]})
    for t in pros:
        t["logo_grande"] = url_foto(t["logo_file_id"], 480) if t.get("logo_file_id") else None
        t["galeria"] = galerias[t["id"]]
        t["mais"] = max(0, t["n_imoveis"] - len(t["galeria"]))
        t["finalidades"] = [FINALIDADES[f] for f in fins[t["id"]]]
    return render_template("imobiliarias.html", lista=lista, q=q)


@app.route("/imobiliarias/<slug>")
def imobiliaria(slug):
    t = query_one("SELECT * FROM tenants WHERE slug = %s AND status = 'ativo'", (slug,))
    if not t:
        abort(404)
    enriquecer_tenant(t)
    lista, total, pagina, paginas, f = consulta_imoveis(request.args, "i.tenant_id = %s", (t["id"],), por_pagina=9)
    dono = bool(g.tenant and g.tenant["id"] == t["id"]) or bool(g.usuario and g.usuario["tipo"] == "admin")
    tel_ok = dono or (email_ok(g.usuario) and slug in session.get("_tel", []))
    ctx = dict(tn=t, imoveis=lista, total=total, pagina=pagina, paginas=paginas,
               faixa=_faixa_paginas(pagina, paginas), f=f, qs=_montar_qs(request.args),
               pro=t["plano_efetivo"] == "profissional", tel_ok=tel_ok)
    caminho = template_do_tenant(t)
    try:
        return render_template(caminho, **ctx)
    except Exception:
        padrao = "paginas/%s.html" % TEMPLATE_PADRAO
        if caminho == padrao:
            raise
        log.exception("Template de página %s falhou (tenant %s); usando o padrão", caminho, t["id"])
        return render_template(padrao, **ctx)


def _tenant_contato(slug):
    t = query_one("SELECT * FROM tenants WHERE slug = %s AND status = 'ativo'", (slug,))
    if not t:
        abort(404)
    return t


@app.route("/imobiliarias/<slug>/whatsapp")
@verificado_required
def imobiliaria_whatsapp(slug):
    t = _tenant_contato(slug)
    if not so_digitos(t["whatsapp"]):
        flash("Este anunciante ainda não informou um WhatsApp.", "error")
        return redirect(url_for("imobiliaria", slug=slug))
    execute("INSERT INTO eventos (tenant_id, usuario_id, tipo) VALUES (%s,%s,'whatsapp')", (t["id"], g.usuario["id"]))
    registrar_lead("imobiliaria", "whatsapp", t["id"])
    return redirect(wa_link(t["whatsapp"], "Olá! Vi sua página no OndeImóveis."))


@app.route("/imobiliarias/<slug>/telefone", methods=["POST"])
@verificado_required
def imobiliaria_telefone(slug):
    t = _tenant_contato(slug)
    registrar_lead("imobiliaria", "telefone", t["id"])
    liberados = session.get("_tel", [])
    if slug not in liberados:
        session["_tel"] = (liberados + [slug])[-30:]
    return redirect(url_for("imobiliaria", slug=slug) + "#contato")


@app.route("/perto-de-mim")
def perto_de_mim():
    return render_template("perto.html")


@app.route("/api/perto")
def api_perto():
    """Imóveis num raio (haversine). Usado pela página 'Perto de mim'."""
    try:
        lat, lng = float(request.args["lat"]), float(request.args["lng"])
    except (KeyError, ValueError):
        return jsonify(erro="lat/lng inválidos"), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify(erro="coordenadas fora do intervalo"), 400
    raio = min(max(request.args.get("raio", 5, type=float), 1), 50) * 1000
    finalidade = request.args.get("finalidade", "")
    tipo = request.args.get("tipo", "")
    preco_max = num_br(request.args.get("preco_max"))
    ordem = {"preco": "preco ASC", "recentes": "criado_em DESC"}.get(request.args.get("ordem"), "dist ASC")
    filtros, params = "", [lat, lng, lat]
    if finalidade in FINALIDADES:
        filtros += " AND finalidade = %s"; params.append(finalidade)
    if tipo in TIPOS_IMOVEL:
        filtros += " AND tipo = %s"; params.append(tipo)
    if preco_max:
        filtros += " AND preco <= %s"; params.append(preco_max)
    linhas = query_all(
        f"""SELECT * FROM (SELECT i.id, i.slug, i.titulo, i.preco, i.finalidade, i.tipo, i.bairro, i.area, i.dormitorios,
                   i.lat, i.lng, i.criado_em, {CAPA_SQL} AS capa,
                   6371000 * acos(LEAST(1, cos(radians(%s)) * cos(radians(i.lat)) * cos(radians(i.lng) - radians(%s))
                                  + sin(radians(%s)) * sin(radians(i.lat)))) AS dist
              FROM imoveis i JOIN tenants t ON t.id = i.tenant_id
             WHERE i.status = 'publicado' AND t.status = 'ativo' AND i.lat IS NOT NULL AND i.lng IS NOT NULL) x
            WHERE dist <= %s {filtros} ORDER BY {ordem} LIMIT 60""", [params[0], params[1], params[2], raio, *params[3:]])
    return jsonify(total=len(linhas), imoveis=[{
        "slug": r["slug"], "titulo": r["titulo"], "preco": preco_txt(r), "finalidade": FINALIDADES[r["finalidade"]],
        "tipo": TIPOS_IMOVEL[r["tipo"]], "bairro": r["bairro"], "area": float(r["area"]) if r["area"] else None,
        "dormitorios": r["dormitorios"], "lat": r["lat"], "lng": r["lng"], "dist": round(r["dist"]),
        "foto": url_foto(r["capa"], 320), "url": url_for("imovel", slug=r["slug"])} for r in linhas])


def _rua_sem_numero(rua):
    """'Rua das Flores, 120' -> 'Rua das Flores'. Mantém nomes como 'Rua 15' (só uma palavra antes do número)."""
    base = re.sub(r"(,\s*|\s+)(n[ºo°.]*\s*)?\d+[a-zA-Z]?\s*$", "", rua or "").strip(" ,")
    return base if " " in base else (rua or "").strip(" ,")


_GEO_RUIM = {"city", "town", "village", "hamlet", "suburb", "neighbourhood", "quarter", "city_district", "borough",
             "state", "county", "region", "municipality", "country", "postcode", "district"}


def _nominatim(**params):
    r = requests.get("https://nominatim.openstreetmap.org/search",
                     params={"format": "jsonv2", "limit": 3, "countrycodes": "br", **params},
                     headers={"User-Agent": f"{SITE_NOME}/1.0 ({ADMIN_EMAIL or BASE_URL or 'portal imobiliario'})",
                              "Accept-Language": "pt-BR"}, timeout=6)
    r.raise_for_status()
    # descarta resultados que são só cidade/bairro/estado: queremos a rua ou o imóvel
    return [d for d in r.json() if d.get("category") != "boundary" and d.get("type") not in _GEO_RUIM
            and d.get("addresstype") not in _GEO_RUIM]


@app.route("/api/geocodificar")
@login_required
def api_geocodificar():
    """Endereço → latitude/longitude (OpenStreetMap/Nominatim). Usado no cadastro do imóvel.
    Tenta várias formas (com/sem bairro, com/sem número) porque o OSM é irregular no Brasil."""
    rua = sanitize_input(request.args.get("rua", ""))[:150]
    bairro = sanitize_input(request.args.get("bairro", ""))[:100]
    cidade = sanitize_input(request.args.get("cidade", ""))[:100]
    uf = sanitize_input(request.args.get("uf", ""))[:2]
    q = sanitize_input(request.args.get("q", ""))[:200]
    if len(rua or q) < 3:
        return jsonify(ok=False, erro="Digite a rua e o número."), 400
    agora = time.time()
    if agora - session.get("_geo_t", 0) < 1.1:                 # política do Nominatim: no máx. 1 consulta/segundo
        return jsonify(ok=False, erro="Calma, uma busca por vez. Tente de novo."), 429
    session["_geo_t"] = agora

    sem_numero = _rua_sem_numero(rua) if rua else ""
    cidade_uf = ", ".join(x for x in (cidade, uf) if x)
    tentativas = []                                             # (parâmetros, aproximado?)
    if rua:
        if cidade:
            est = {"street": rua.replace(",", " "), "city": cidade, "country": "Brazil"}
            if uf:
                est["state"] = uf
            tentativas.append((est, False))
        tentativas.append(({"q": ", ".join(x for x in (rua, bairro, cidade_uf) if x)}, False))
        if bairro:
            tentativas.append(({"q": ", ".join(x for x in (rua, cidade_uf) if x)}, False))
        if sem_numero and sem_numero != rua:
            if cidade:
                est = {"street": sem_numero, "city": cidade, "country": "Brazil"}
                if uf:
                    est["state"] = uf
                tentativas.append((est, True))
            tentativas.append(({"q": ", ".join(x for x in (sem_numero, bairro, cidade_uf) if x)}, True))
    else:
        tentativas.append(({"q": q}, False))

    vistos, falhas, d, aprox = set(), 0, None, False
    for params, aprox_t in tentativas:
        chave = json.dumps(params, sort_keys=True)
        if chave in vistos:
            continue
        vistos.add(chave)
        if len(vistos) > 1:
            time.sleep(1.05)                                    # respeita 1 consulta/segundo
        try:
            achados = _nominatim(**params)
        except Exception:
            falhas += 1
            log.exception("geocodificação falhou")
            continue
        if achados:
            d, aprox = achados[0], aprox_t
            break
    if not d:
        if falhas == len(vistos):
            return jsonify(ok=False, erro="Não consegui consultar o mapa agora. Tente de novo ou use “Usar minha localização atual”."), 502
        return jsonify(ok=False, erro="Não achei essa rua no mapa. Confira cidade e UF, tente sem o número, ou preencha latitude/longitude "
                                      "(Google Maps: botão direito no local → copiar coordenadas)."), 404
    return jsonify(ok=True, lat=round(float(d["lat"]), 6), lng=round(float(d["lon"]), 6),
                   rotulo=d.get("display_name", ""), aprox=aprox)


@app.route("/anuncie-seu-imovel")
def anuncie():
    return render_template("anuncie.html", limites=LIMITES)


@app.route("/para-empresas", strict_slashes=False)
def para_empresas():
    return render_template("para_empresas.html", seo_canonical=base_url() + url_for("para_empresas"))


@app.route("/para-empresas/sites-imobiliarios", strict_slashes=False)
def empresas_sites():
    return render_template("empresas_sites.html", seo_canonical=base_url() + url_for("empresas_sites"))


@app.route("/para-empresas/trafego-pago", strict_slashes=False)
def empresas_trafego():
    return render_template("empresas_trafego.html", seo_canonical=base_url() + url_for("empresas_trafego"))


# ═══════════════════════════════════════════════════════════════
# 4b. GUIA / BLOG — artigos, notícias e guias de bairro (feito para SEO)
#     Tabelas: guia_posts, guia_categorias, guia_tags, guia_post_tags, guia_autores, guia_redirecionamentos.
#     Público: /guia · /guia/<slug> · /guia/categoria/<slug> · /guia/tag/<slug> · /guia/autor/<slug>
#              /guia/busca · /guia/feed.xml        Admin: /admin/guia
# ═══════════════════════════════════════════════════════════════

GUIA_MARCA = SITE_NOME
TIPOS_GUIA = {"artigo": "Artigo", "noticia": "Notícia", "bairro": "Guia de bairro"}
STATUS_GUIA = {"rascunho": "Rascunho", "agendado": "Agendado", "publicado": "Publicado"}
GUIA_POR_PAGINA = 12
GUIA_RESERVADOS = {"categoria", "tag", "autor", "busca", "feed", "rss", "novo", "sitemap"}   # não podem ser slug de post
# "no ar" = publicado (ou agendado cuja hora já chegou). Posts agendados entram sozinhos, sem cron.
GUIA_PUB = "p.status IN ('publicado', 'agendado') AND p.publicado_em IS NOT NULL AND p.publicado_em <= NOW()"
GUIA_SEL = ("SELECT p.id, p.titulo, p.slug, p.resumo, p.tipo, p.capa, p.capa_alt, p.publicado_em, p.atualizado_em, "
            "p.tempo_leitura, p.destaque, p.categoria_id, p.autor_id, c.nome AS categoria_nome, c.slug AS categoria_slug, "
            "a.nome AS autor_nome, a.slug AS autor_slug "
            "FROM guia_posts p LEFT JOIN guia_categorias c ON c.id = p.categoria_id AND c.ativa "
            "LEFT JOIN guia_autores a ON a.id = p.autor_id")
GUIA_SEL_FULL = ("SELECT p.*, c.nome AS categoria_nome, c.slug AS categoria_slug, a.nome AS autor_nome, a.slug AS autor_slug, "
                 "a.cargo AS autor_cargo, a.bio AS autor_bio, a.foto AS autor_foto "
                 "FROM guia_posts p LEFT JOIN guia_categorias c ON c.id = p.categoria_id AND c.ativa "
                 "LEFT JOIN guia_autores a ON a.id = p.autor_id")


# ── limpeza do HTML do editor (lista branca: nada de script, iframe, onclick, style…) ──

_G_PERMITIDAS = {"p", "h2", "h3", "h4", "ul", "ol", "li", "strong", "em", "u", "a", "blockquote", "img", "figure",
                 "figcaption", "br", "hr", "table", "thead", "tbody", "tr", "th", "td", "code", "pre", "sup", "sub", "s", "mark"}
_G_BLOCOS = {"p", "h2", "h3", "h4", "ul", "ol", "blockquote", "figure", "table", "hr", "pre"}
_G_SEM_TEXTO = {"ul", "ol", "table", "thead", "tbody", "tr"}
_G_VAZIAS = {"br", "hr", "img"}
_G_APELIDOS = {"b": "strong", "i": "em", "h1": "h2", "h5": "h4", "h6": "h4", "strike": "s", "del": "s",
               "div": "p", "section": "p", "article": "p"}
_G_DESCARTAR = {"script", "style", "iframe", "object", "embed", "svg", "math", "noscript", "template", "textarea",
                "select", "head", "title"}


def _g_dimensoes(src, largura_url):
    """width/height de uma foto do volume (evita o texto 'pular' quando a imagem carrega)."""
    m = re.match(r"^/assets/([a-f0-9]{32})", src)
    if not m:
        return None
    r = query_one("SELECT largura, altura FROM arquivos WHERE id = %s", (m.group(1),))
    if not r or not r["largura"] or not r["altura"]:
        return None
    w = min(largura_url or r["largura"], r["largura"])
    return w, max(1, round(r["altura"] * w / r["largura"]))


class _GuiaLimpa(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.pilha = [], []
        self.ignorar, self.ignorar_tag = 0, ""
        self.host = urlparse(BASE_URL).netloc.lower() if BASE_URL else ""

    def _abrir_p(self):
        if not self.pilha:
            self.out.append("<p>")
            self.pilha.append("p")

    def _fechar_ate(self, tag):
        while self.pilha:
            t = self.pilha.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.ignorar:
            if tag == self.ignorar_tag:
                self.ignorar += 1
            return
        if tag in _G_DESCARTAR:
            self.ignorar, self.ignorar_tag = 1, tag
            return
        tag = _G_APELIDOS.get(tag, tag)
        if tag not in _G_PERMITIDAS:
            return                                           # tag desconhecida: some, o texto fica
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in _G_BLOCOS and "p" in self.pilha:
            self._fechar_ate("p")
        if tag == "li" and self.pilha and self.pilha[-1] == "li":
            self._fechar_ate("li")
        if tag == "br":
            if self.pilha:
                self.out.append("<br>")
            return
        if tag == "hr":
            self.out.append("<hr>")
            return
        if tag == "img":
            src = a.get("src", "").strip()
            if not re.match(r"^(https://|/assets/[a-f0-9]{32}|/site-media/)", src):
                return
            self._abrir_p()
            dim = _g_dimensoes(src, int((re.search(r"[?&]w=(\d+)", src) or [0, 0])[1]) or None)
            self.out.append(f'<img src="{html_escape(src)}" alt="{html_escape(a.get("alt", "")[:200])}"'
                            + (f' width="{dim[0]}" height="{dim[1]}"' if dim else "") + ' loading="lazy" decoding="async">')
            return
        if tag == "a":
            href = a.get("href", "").strip()
            if not re.match(r"^(https?://|mailto:|tel:|/(?!/)|#)", href, re.I):
                return                                       # link sem endereço seguro vira só texto
            interno = href.startswith(("/", "#")) or (self.host and urlparse(href).netloc.lower() == self.host)
            self._abrir_p()
            extra = "" if interno else ' target="_blank" rel="noopener"'
            titulo = f' title="{html_escape(a["title"][:150])}"' if a.get("title") else ""
            self.out.append(f'<a href="{html_escape(href)}"{titulo}{extra}>')
            self.pilha.append("a")
            return
        if tag not in _G_BLOCOS and tag not in ("li", "figcaption", "tr", "thead", "tbody", "th", "td"):
            self._abrir_p()                                  # inline solto (strong, em…) no topo ganha um <p>
        attrs_ok = ""
        if tag in ("th", "td"):
            for k in ("colspan", "rowspan"):
                if a.get(k, "").isdigit() and 1 < int(a[k]) <= 12:
                    attrs_ok += f' {k}="{int(a[k])}"'
        self.out.append(f"<{tag}{attrs_ok}>")
        self.pilha.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _G_VAZIAS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.ignorar:
            if tag == self.ignorar_tag:
                self.ignorar -= 1
            return
        tag = _G_APELIDOS.get(tag, tag)
        if tag in _G_VAZIAS or tag not in self.pilha:
            return
        self._fechar_ate(tag)

    def handle_data(self, data):
        if self.ignorar:
            return
        if self.pilha and self.pilha[-1] in _G_SEM_TEXTO:
            return
        data = data.replace("\xa0", " ")
        if not self.pilha:
            if not data.strip():
                return
            self._abrir_p()
        self.out.append(str(html_escape(data)))


def guia_limpar_html(html):
    p = _GuiaLimpa()
    p.feed(str(html or ""))
    p.close()
    p._fechar_ate("")
    saida = "".join(p.out)
    saida = re.sub(r"<p>(?:\s|<br>)*</p>", "", saida)
    return saida.strip()


def guia_ids_titulos(html):
    """Põe id em cada <h2>/<h3> (âncoras para o sumário e para o Google)."""
    usados = set()

    def sub(m):
        nivel, inner = m.group(1), m.group(2)
        texto = html_unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        base = gerar_slug(texto)[:60] if texto else "secao"
        id_, n = base, 1
        while id_ in usados:
            n += 1
            id_ = f"{base}-{n}"
        usados.add(id_)
        return f'<h{nivel} id="{id_}">{inner}</h{nivel}>'
    return re.sub(r"<h([23])(?:\s[^>]*)?>(.*?)</h\1>", sub, html, flags=re.S)


def guia_toc(html):
    return [{"nivel": int(m.group(1)), "id": m.group(2), "texto": html_unescape(re.sub(r"<[^>]+>", "", m.group(3))).strip()}
            for m in re.finditer(r'<h([23]) id="([^"]+)">(.*?)</h\1>', html or "", flags=re.S)]


def guia_processar(html):
    """HTML do editor → (html limpo com ids nos títulos, nº de palavras, texto puro)."""
    limpo = guia_ids_titulos(guia_limpar_html(html))
    texto = re.sub(r"\s+", " ", html_unescape(re.sub(r"<[^>]+>", " ", limpo))).strip()
    return limpo, len(texto.split()), texto


def guia_resumo_auto(texto, n=160):
    texto = re.sub(r"\s+", " ", texto or "").strip()
    if len(texto) <= n:
        return texto
    return texto[:n].rsplit(" ", 1)[0].rstrip(",;:.") + "…"


def guia_capa_url(capa, w=None):
    """capa pode ser o id de uma foto do volume ou uma URL https."""
    capa = (capa or "").strip()
    if not capa:
        return None
    if re.fullmatch(r"[a-f0-9]{32}", capa):
        return url_foto(capa, w)
    if re.match(r"^(https?://|/(?!/))", capa):
        return capa
    return None


def _abs_url(u):
    return (base_url() + u) if u and u.startswith("/") else u


def guia_cards(lista, largura=640):
    for p in lista:
        p["img"] = guia_capa_url(p.get("capa"), largura) or url_for("placeholder")
    return lista


def guia_slug_livre(tabela, base, ignorar_id=None):
    assert tabela in ("guia_posts", "guia_categorias", "guia_autores")
    base = base or "item"
    slug, n = base, 1
    while True:
        r = query_one(f"SELECT id FROM {tabela} WHERE slug = %s", (slug,))
        if (not r or r["id"] == ignorar_id) and not (tabela == "guia_posts" and slug in GUIA_RESERVADOS):
            return slug
        n += 1
        slug = f"{base}-{n}"


def guia_autor_padrao():
    r = query_one("SELECT id FROM guia_autores ORDER BY id LIMIT 1")
    if r:
        return r["id"]
    nome = f"Equipe {GUIA_MARCA}"
    return execute_returning("INSERT INTO guia_autores (nome, slug) VALUES (%s, %s) ON CONFLICT (slug) DO UPDATE "
                             "SET nome = guia_autores.nome RETURNING id", (nome, gerar_slug(nome)))


def _guia_jsonld(obj):
    return Markup(json.dumps(obj, ensure_ascii=False).replace("</", "<\\/"))


def _guia_seo(titulo, descricao, canonical=None, robots="index,follow", og_type="website", imagem=None, **extra):
    return dict(titulo=titulo, descricao=guia_resumo_auto(descricao, 300), canonical=canonical or canonical_url(),
                robots=robots, og_type=og_type, imagem=_abs_url(imagem) or _site_ctx()["og"], **extra)


_SEO_DUPLICADO = (r"<title>.*?</title>\s*",
                  r"<meta[^>]+name=[\"'](?:description|robots)[\"'][^>]*>\s*",
                  r"<meta[^>]+property=[\"']og:[^\"']+[\"'][^>]*>\s*",
                  r"<meta[^>]+name=[\"']twitter:[^\"']+[\"'][^>]*>\s*",
                  r"<link[^>]+rel=[\"']canonical[\"'][^>]*>\s*")


def render_guia(tpl, seo, **ctx):
    """Renderiza uma página do guia. O bloco de SEO do guia (entre <!--guia-seo--> …) manda: o que o _seo.html
    do site já tiver posto de title/description/canonical/og/twitter/robots antes dele é removido para não duplicar
    (o resto do _seo.html — analytics, verificação do Search Console etc. — continua)."""
    ctx.update(seo=seo, seo_title=seo["titulo"], seo_desc=seo["descricao"], tipos=TIPOS_GUIA)
    html = render_template(tpl, **ctx)
    if "<!--guia-seo-->" in html:
        antes, depois = html.split("<!--guia-seo-->", 1)
        for padrao in _SEO_DUPLICADO:
            antes = re.sub(padrao, "", antes, flags=re.S | re.I)
        html = antes + "<!--guia-seo-->" + depois
    return html


def _guia_pagina():
    return max(1, request.args.get("pagina", 1, type=int) or 1)


def _guia_pag_url(n):
    args = {"q": request.args["q"]} if request.args.get("q") else {}
    if n > 1:
        args["pagina"] = n
    return url_for(request.endpoint, **(request.view_args or {}), **args)


def _guia_pag_seo(pagina, paginas):
    base = base_url() + url_for(request.endpoint, **(request.view_args or {}))
    q = f"q={quote(request.args['q'])}" if request.args.get("q") else ""
    url = lambda n: base + ("?" + "&".join(x for x in (q, f"pagina={n}" if n > 1 else "") if x) if (q or n > 1) else "")
    return dict(canonical=url(pagina), prev=url(pagina - 1) if pagina > 1 else None,
                next=url(pagina + 1) if pagina < paginas else None)


def _guia_listar(where="TRUE", params=(), ordem="p.publicado_em DESC, p.id DESC", ordem_params=(), por_pagina=GUIA_POR_PAGINA):
    pagina = _guia_pagina()
    total = query_one(f"SELECT COUNT(*) AS n FROM guia_posts p WHERE {GUIA_PUB} AND {where}", list(params))["n"]
    paginas = max(1, math.ceil(total / por_pagina))
    if pagina > paginas:
        abort(404)
    lista = query_all(f"{GUIA_SEL} WHERE {GUIA_PUB} AND {where} ORDER BY {ordem} LIMIT %s OFFSET %s",
                      list(params) + list(ordem_params) + [por_pagina, (pagina - 1) * por_pagina])
    return guia_cards(lista), pagina, paginas, total


def guia_categorias_ativas():
    return query_all(f"SELECT c.nome, c.slug, COUNT(p.id) AS n FROM guia_categorias c "
                     f"LEFT JOIN guia_posts p ON p.categoria_id = c.id AND {GUIA_PUB} WHERE c.ativa "
                     f"GROUP BY c.id HAVING COUNT(p.id) > 0 ORDER BY c.ordem, c.nome")


@app.route("/guia")
def guia():
    pagina = _guia_pagina()
    destaques = query_all(f"{GUIA_SEL} WHERE {GUIA_PUB} AND p.destaque ORDER BY p.ordem_destaque, p.publicado_em DESC LIMIT 3")
    for i, p in enumerate(destaques):
        p["img"] = guia_capa_url(p["capa"], 1200 if i == 0 else 320) or url_for("placeholder")
    lista, pagina, paginas, total = _guia_listar("p.id <> ALL(%s)", [[p["id"] for p in destaques]])
    bairros = guia_cards(query_all(f"{GUIA_SEL} WHERE {GUIA_PUB} AND p.tipo = 'bairro' ORDER BY p.publicado_em DESC LIMIT 6"), 640) \
        if pagina == 1 else []
    seo = _guia_seo("Guia de bairros e dicas para escolher seu imóvel | " + GUIA_MARCA,
                    "Guias de bairros, dicas para comprar ou alugar, notícias e informações para escolher melhor o seu próximo imóvel.",
                    **_guia_pag_seo(pagina, paginas), jsonld=_guia_jsonld({
                        "@context": "https://schema.org", "@type": "CollectionPage", "name": f"Guia {GUIA_MARCA}",
                        "url": base_url() + url_for("guia"), "isPartOf": {"@type": "WebSite", "name": GUIA_MARCA, "url": base_url() + "/"}}))
    return render_guia("guia.html", seo, destaques=destaques, lista=lista, bairros=bairros, categorias=guia_categorias_ativas(),
                       pagina=pagina, paginas=paginas, total=total, faixa=_faixa_paginas(pagina, paginas), pag_url=_guia_pag_url)


def _guia_pagina_lista(modo, titulo, descricao, where, params, h1, seo_titulo=None, seo_descricao=None, indexar=True, **ctx):
    lista, pagina, paginas, total = _guia_listar(where, params, **ctx.pop("_listar", {}))
    pag = _guia_pag_seo(pagina, paginas)
    seo = _guia_seo(seo_titulo or f"{titulo} | {GUIA_MARCA}", seo_descricao or descricao,
                    robots="index,follow" if (indexar and total) else "noindex,follow", **pag)
    return render_guia("guia_lista.html", seo, modo=modo, h1=h1, descricao=descricao, lista=lista, pagina=pagina, paginas=paginas,
                       total=total, faixa=_faixa_paginas(pagina, paginas), pag_url=_guia_pag_url,
                       categorias=guia_categorias_ativas(), **ctx)


@app.route("/guia/categoria/<slug>")
def guia_categoria(slug):
    c = query_one("SELECT * FROM guia_categorias WHERE slug = %s AND ativa", (slug,))
    if not c:
        abort(404)
    desc = c["descricao"] or f"Artigos e guias sobre {c['nome'].lower()} no {GUIA_MARCA}."
    return _guia_pagina_lista("categoria", c["nome"], desc, "p.categoria_id = %s", [c["id"]], h1=c["nome"],
                              seo_titulo=c["seo_titulo"] or None, seo_descricao=c["seo_descricao"] or None, categoria=c)


@app.route("/guia/tag/<slug>")
def guia_tag(slug):
    t = query_one("SELECT * FROM guia_tags WHERE slug = %s", (slug,))
    if not t:
        abort(404)
    n = query_one(f"SELECT COUNT(*) AS n FROM guia_post_tags pt JOIN guia_posts p ON p.id = pt.post_id "
                  f"WHERE pt.tag_id = %s AND {GUIA_PUB}", (t["id"],))["n"]
    return _guia_pagina_lista("tag", f"{t['nome']} — artigos", f"Tudo o que publicamos sobre {t['nome']}.",
                              "EXISTS (SELECT 1 FROM guia_post_tags pt WHERE pt.post_id = p.id AND pt.tag_id = %s)", [t["id"]],
                              h1=t["nome"], indexar=n >= 2, tag=t)


@app.route("/guia/autor/<slug>")
def guia_autor(slug):
    a = query_one("SELECT * FROM guia_autores WHERE slug = %s", (slug,))
    if not a:
        abort(404)
    a["foto_url"] = guia_capa_url(a["foto"], 240)
    desc = a["bio"] or f"Artigos escritos por {a['nome']} no {GUIA_MARCA}."
    return _guia_pagina_lista("autor", f"{a['nome']}, autor", desc, "p.autor_id = %s", [a["id"]], h1=a["nome"], autor=a)


@app.route("/guia/busca")
def guia_busca():
    q = sanitize_input(request.args.get("q", ""))[:80]
    if not q:
        return redirect(url_for("guia"))
    ts = "to_tsvector('portuguese', coalesce(p.titulo,'') || ' ' || coalesce(p.resumo,'') || ' ' || coalesce(p.corpo,''))"
    return _guia_pagina_lista("busca", f"Busca: {q}", f"Resultados do guia para “{q}”.",
                              f"({ts} @@ websearch_to_tsquery('portuguese', %s) OR p.titulo ILIKE %s)", [q, f"%{q}%"],
                              h1=f"Resultados para “{q}”", indexar=False, q=q,
                              _listar=dict(ordem="(p.titulo ILIKE %s) DESC, p.publicado_em DESC", ordem_params=[f"%{q}%"]))


def _eh_robo():
    return bool(re.search(r"bot|crawl|spider|slurp|preview|facebookexternalhit|curl|python-requests",
                          request.headers.get("User-Agent", ""), re.I))


def guia_imoveis_relacionados(p, n=3):
    """Até 3 imóveis do mesmo bairro (ou cidade) do post — liga o conteúdo ao anúncio (link interno + conversão)."""
    if not p.get("cidade_slug"):
        return [], None, None
    for bairro in ([p["bairro_slug"]] if p.get("bairro_slug") else []) + [None]:
        cond, params = "i.status = 'publicado' AND t.status = 'ativo' AND i.cidade_slug = %s", [p["cidade_slug"]]
        if bairro:
            cond += " AND i.bairro_slug = %s"
            params.append(bairro)
        lista = query_all(f"SELECT i.*, {CAPA_SQL} AS capa FROM imoveis i JOIN tenants t ON t.id = i.tenant_id WHERE {cond} "
                          f"ORDER BY {sql_destaque()} DESC, i.criado_em DESC LIMIT %s", params + [n])
        if lista:
            rotulo = (lista[0]["bairro"] if bairro else lista[0]["cidade"])
            return preparar_imoveis(lista, 480), url_local(p["cidade_slug"], bairro), rotulo
    return [], None, None


def _guia_render_post(p, previa=False):
    tags = query_all("SELECT t.nome, t.slug FROM guia_post_tags pt JOIN guia_tags t ON t.id = pt.tag_id "
                     "WHERE pt.post_id = %s ORDER BY t.nome", (p["id"],))
    rel = query_all(f"{GUIA_SEL} WHERE {GUIA_PUB} AND p.id <> %s AND (p.categoria_id = %s OR EXISTS (SELECT 1 FROM guia_post_tags a "
                    f"JOIN guia_post_tags b ON b.tag_id = a.tag_id WHERE a.post_id = p.id AND b.post_id = %s)) "
                    f"ORDER BY (p.categoria_id = %s) DESC NULLS LAST, p.publicado_em DESC LIMIT 3",
                    (p["id"], p["categoria_id"], p["id"], p["categoria_id"]))
    if len(rel) < 3:
        rel += query_all(f"{GUIA_SEL} WHERE {GUIA_PUB} AND p.id <> ALL(%s) ORDER BY p.publicado_em DESC LIMIT %s",
                         ([p["id"]] + [r["id"] for r in rel], 3 - len(rel)))
    imoveis, imoveis_url, imoveis_rotulo = guia_imoveis_relacionados(p)
    if not previa and not _eh_robo() and not (g.usuario and g.usuario["tipo"] == "admin"):
        execute("UPDATE guia_posts SET visualizacoes = visualizacoes + 1 WHERE id = %s", (p["id"],))
    url = base_url() + url_for("guia_post", slug=p["slug"])
    capa = guia_capa_url(p["capa"], 1200)
    texto = re.sub(r"\s+", " ", html_unescape(re.sub(r"<[^>]+>", " ", p["corpo"] or ""))).strip()
    desc = p["seo_descricao"] or p["resumo"] or guia_resumo_auto(texto)
    titulo_seo = p["seo_titulo"] or (f"{p['titulo']} | {GUIA_MARCA}" if len(p["titulo"]) <= 58 else p["titulo"])
    pub = _local(p["publicado_em"]) if p["publicado_em"] else None
    upd = _local(p["atualizado_em"]) if p["atualizado_em"] else pub
    p["pub_iso"], p["upd_iso"] = (pub.isoformat() if pub else ""), (upd.isoformat() if upd else "")
    p["atualizado_depois"] = bool(pub and upd and (upd - pub).days >= 1)
    p["autor_foto_url"] = guia_capa_url(p.get("autor_foto"), 120)
    crumbs = [("Início", base_url() + "/"), ("Guia", base_url() + url_for("guia"))]
    if p["categoria_slug"]:
        crumbs.append((p["categoria_nome"], base_url() + url_for("guia_categoria", slug=p["categoria_slug"])))
    crumbs.append((p["titulo"], url))
    logo = _site_ctx()["logo_abs"]
    grafo = [{"@type": "NewsArticle" if p["tipo"] == "noticia" else "Article", "@id": url + "#artigo",
              "mainEntityOfPage": {"@type": "WebPage", "@id": url}, "headline": p["titulo"][:110], "description": desc,
              "image": [_abs_url(capa)] if capa else ([_site_ctx()["og"]] if _site_ctx()["og"] else []),
              "datePublished": p["pub_iso"], "dateModified": p["upd_iso"], "inLanguage": "pt-BR",
              "wordCount": len(texto.split()),
              **({"articleSection": p["categoria_nome"]} if p["categoria_nome"] else {}),
              **({"keywords": ", ".join(t["nome"] for t in tags)} if tags else {}),
              "author": ({"@type": "Person", "name": p["autor_nome"], "url": base_url() + url_for("guia_autor", slug=p["autor_slug"])}
                         if p["autor_nome"] else {"@type": "Organization", "name": GUIA_MARCA}),
              "publisher": {"@type": "Organization", "name": GUIA_MARCA, "url": base_url() + "/",
                            **({"logo": {"@type": "ImageObject", "url": logo}} if logo else {})}},
             {"@type": "BreadcrumbList", "itemListElement": [
                 {"@type": "ListItem", "position": i + 1, "name": n, "item": u} for i, (n, u) in enumerate(crumbs)]}]
    seo = _guia_seo(titulo_seo, desc, canonical=url, og_type="article", imagem=capa,
                    robots="noindex,nofollow" if previa else ("noindex,follow" if p["noindex"] else "index,follow"),
                    publicado=p["pub_iso"], modificado=p["upd_iso"], secao=p["categoria_nome"], tags=[t["nome"] for t in tags],
                    jsonld=_guia_jsonld({"@context": "https://schema.org", "@graph": grafo}))
    corpo = p["corpo"] or ""
    toc = guia_toc(corpo)
    txt = quote(f"{p['titulo']} {url}")
    share = [("WhatsApp", f"https://wa.me/?text={txt}"), ("Facebook", f"https://www.facebook.com/sharer/sharer.php?u={quote(url)}"),
             ("X", f"https://twitter.com/intent/tweet?text={quote(p['titulo'])}&url={quote(url)}"),
             ("LinkedIn", f"https://www.linkedin.com/sharing/share-offsite/?url={quote(url)}")]
    return render_guia("guia_post.html", seo, p=p, corpo=Markup(corpo), toc=toc if len(toc) >= 3 else [], tags=tags, relacionados=guia_cards(rel),
                       imoveis=imoveis, imoveis_url=imoveis_url, imoveis_rotulo=imoveis_rotulo, capa_url=capa, share=share,
                       url_post=url, crumbs=crumbs, previa=previa)


@app.route("/guia/<slug>")
def guia_post(slug):
    p = query_one(f"{GUIA_SEL_FULL} WHERE p.slug = %s AND {GUIA_PUB}", (slug,))
    if not p:
        r = query_one(f"SELECT p.slug FROM guia_redirecionamentos r JOIN guia_posts p ON p.id = r.post_id "
                      f"WHERE r.slug_antigo = %s AND {GUIA_PUB}", (slug,))
        if r:
            return redirect(url_for("guia_post", slug=r["slug"]), 301)      # slug mudou: 301 preserva o SEO
        abort(404)
    return _guia_render_post(p)


@app.route("/guia/feed.xml")
def guia_feed():
    posts = query_all(f"SELECT p.titulo, p.slug, p.resumo, p.corpo, p.publicado_em, p.atualizado_em, c.nome AS categoria_nome, "
                      f"a.nome AS autor_nome FROM guia_posts p LEFT JOIN guia_categorias c ON c.id = p.categoria_id "
                      f"LEFT JOIN guia_autores a ON a.id = p.autor_id WHERE {GUIA_PUB} AND NOT p.noindex "
                      f"ORDER BY p.publicado_em DESC LIMIT 30")
    base = base_url()
    itens = ""
    for p in posts:
        link = base + url_for("guia_post", slug=p["slug"])
        itens += (f"<item><title>{xml_escape(p['titulo'])}</title><link>{xml_escape(link)}</link>"
                  f"<guid isPermaLink=\"true\">{xml_escape(link)}</guid><pubDate>{format_datetime(p['publicado_em'])}</pubDate>"
                  + (f"<category>{xml_escape(p['categoria_nome'])}</category>" if p["categoria_nome"] else "")
                  + (f"<dc:creator>{xml_escape(p['autor_nome'])}</dc:creator>" if p["autor_nome"] else "")
                  + f"<description>{xml_escape(p['resumo'] or guia_resumo_auto(re.sub(r'<[^>]+>', ' ', p['corpo'] or '')))}</description>"
                  f"<content:encoded><![CDATA[{(p['corpo'] or '').replace(']]>', ']]&gt;')}]]></content:encoded></item>")
    xml = ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" '
           'xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>'
           f"<title>Guia {xml_escape(GUIA_MARCA)}</title><link>{xml_escape(base + url_for('guia'))}</link>"
           "<description>Guias de bairros, dicas e notícias do mercado imobiliário.</description><language>pt-BR</language>"
           f"<atom:link href=\"{xml_escape(base + url_for('guia_feed'))}\" rel=\"self\" type=\"application/rss+xml\"/>{itens}</channel></rss>")
    return Response(xml, mimetype="application/rss+xml", headers={"Cache-Control": "public, max-age=600"})


# ── ADMIN DO GUIA ──────────────────────────────────────────────

def _guia_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _guia_s(v):
    return gerar_slug(v) if str(v or "").strip() else None


def _guia_dt(v):
    """'2026-10-11T14:30' (hora de Brasília, do <input datetime-local>) → datetime UTC."""
    try:
        return datetime.strptime((v or "").strip(), "%Y-%m-%dT%H:%M").replace(tzinfo=TZ).astimezone(timezone.utc)
    except ValueError:
        return None


def _guia_dt_local(dt):
    return _local(dt).strftime("%Y-%m-%dT%H:%M") if dt else ""


def _guia_ler_form(atual):
    f = request.form
    erros = []
    d = {"titulo": sanitize_input(f.get("titulo", ""))[:200], "resumo": sanitize_input(f.get("resumo", ""))[:400],
         "tipo": f.get("tipo") if f.get("tipo") in TIPOS_GUIA else "artigo",
         "capa_alt": sanitize_input(f.get("capa_alt", ""))[:200], "seo_titulo": sanitize_input(f.get("seo_titulo", ""))[:70] or None,
         "seo_descricao": sanitize_input(f.get("seo_descricao", ""))[:200] or None,
         "destaque": f.get("destaque") == "1", "noindex": f.get("noindex") == "1",
         "ordem_destaque": max(0, min(999, _guia_int(f.get("ordem_destaque")) or 0)),
         "uf": (re.sub(r"[^A-Za-z]", "", f.get("uf", ""))[:2].upper() or None),
         "cidade_slug": _guia_s(f.get("cidade_slug")), "bairro_slug": _guia_s(f.get("bairro_slug")),
         "capa": atual["capa"] if atual else None}
    d["slug"] = _guia_s(f.get("slug")) or _guia_s(d["titulo"]) or ""
    if not d["titulo"]:
        erros.append("Dê um título ao post.")
    cat = _guia_int(f.get("categoria_id"))
    d["categoria_id"] = cat if cat and query_one("SELECT 1 FROM guia_categorias WHERE id = %s", (cat,)) else None
    aut = _guia_int(f.get("autor_id"))
    d["autor_id"] = aut if aut and query_one("SELECT 1 FROM guia_autores WHERE id = %s", (aut,)) else None
    d["corpo"], palavras, _ = guia_processar(f.get("corpo", ""))
    d["tempo_leitura"] = max(1, round(palavras / 200)) if palavras else None
    status = f.get("status") if f.get("status") in STATUS_GUIA else "rascunho"
    pub = _guia_dt(f.get("publicado_em"))
    antiga = atual["publicado_em"] if atual else None
    agora_ = agora()
    if status == "rascunho":
        pub = pub or antiga
    elif status == "publicado":
        if pub and pub > agora_ + timedelta(minutes=1):
            status = "agendado"
        else:
            pub = pub or antiga or agora_
    else:  # agendado
        if not pub:
            erros.append("Escolha a data e a hora da publicação agendada.")
        elif pub <= agora_:
            status, pub = "publicado", pub
    if status != "rascunho" and not d["corpo"]:
        erros.append("O texto está vazio — escreva algo antes de publicar (ou salve como rascunho).")
    d["status"], d["publicado_em"] = status, pub
    tags, vistos = [], set()
    for t in re.split(r"[,\n;]", f.get("tags", "")):
        nome = sanitize_input(t)[:40]
        s = _guia_s(nome)
        if nome and s and s not in vistos and len(tags) < 12:
            vistos.add(s)
            tags.append((nome, s))
    return d, tags, erros


_GUIA_CAMPOS = ["titulo", "slug", "resumo", "corpo", "tipo", "capa", "capa_alt", "categoria_id", "autor_id", "status", "destaque",
                "ordem_destaque", "publicado_em", "seo_titulo", "seo_descricao", "noindex", "uf", "cidade_slug", "bairro_slug",
                "tempo_leitura"]


def _guia_locais():
    return query_all("SELECT DISTINCT i.cidade, i.cidade_slug, i.uf, i.bairro, i.bairro_slug FROM imoveis i "
                     "WHERE i.status = 'publicado' ORDER BY i.cidade, i.bairro LIMIT 500")


def _guia_render_editor(post, tags_txt=""):
    return render_template("admin.html", pagina_ativa="guia_editar", post=post, tags_txt=tags_txt,
                           pub_local=_guia_dt_local(post.get("publicado_em")),
                           categorias=query_all("SELECT id, nome FROM guia_categorias ORDER BY ordem, nome"),
                           autores=query_all("SELECT id, nome FROM guia_autores ORDER BY nome"), locais=_guia_locais(),
                           capa_preview=guia_capa_url(post.get("capa"), 480), tipos=TIPOS_GUIA, status_guia=STATUS_GUIA)


@app.route("/admin/guia")
@admin_required
def admin_guia():
    q = sanitize_input(request.args.get("q", ""))[:80]
    status = request.args.get("status", "")
    cat = _guia_int(request.args.get("categoria"))
    pagina = _guia_pagina()
    where, params = ["TRUE"], []
    if q:
        where.append("(p.titulo ILIKE %s OR p.slug ILIKE %s)")
        params += [f"%{q}%"] * 2
    if status == "no_ar":
        where.append(GUIA_PUB)
    elif status == "agendado":
        where.append("p.status = 'agendado' AND p.publicado_em > NOW()")
    elif status == "rascunho":
        where.append("p.status = 'rascunho'")
    if cat:
        where.append("p.categoria_id = %s")
        params.append(cat)
    cond = " AND ".join(where)
    total = query_one(f"SELECT COUNT(*) AS n FROM guia_posts p WHERE {cond}", params)["n"]
    paginas = max(1, math.ceil(total / 40))
    lista = query_all(
        f"SELECT p.id, p.titulo, p.slug, p.status, p.tipo, p.destaque, p.publicado_em, p.atualizado_em, p.visualizacoes, p.capa, "
        f"c.nome AS categoria_nome, a.nome AS autor_nome, ({GUIA_PUB}) AS no_ar FROM guia_posts p "
        f"LEFT JOIN guia_categorias c ON c.id = p.categoria_id LEFT JOIN guia_autores a ON a.id = p.autor_id "
        f"WHERE {cond} ORDER BY p.atualizado_em DESC LIMIT 40 OFFSET %s", params + [(max(1, min(pagina, paginas)) - 1) * 40])
    for p in lista:
        p["thumb"] = guia_capa_url(p["capa"], 200)
    n = query_one(f"SELECT COUNT(*) FILTER (WHERE {GUIA_PUB}) AS no_ar, "
                  f"COUNT(*) FILTER (WHERE p.status = 'agendado' AND p.publicado_em > NOW()) AS agendados, "
                  f"COUNT(*) FILTER (WHERE p.status = 'rascunho') AS rascunhos, COALESCE(SUM(p.visualizacoes), 0) AS views "
                  f"FROM guia_posts p")
    return render_template("admin.html", pagina_ativa="guia", lista=lista, q=q, status=status, cat=cat, n=n, pagina=pagina,
                           paginas=paginas, faixa=_faixa_paginas(pagina, paginas), total=total,
                           categorias=query_all("SELECT id, nome FROM guia_categorias ORDER BY ordem, nome"), status_guia=STATUS_GUIA,
                           tipos=TIPOS_GUIA)


@app.route("/admin/guia/novo", methods=["GET", "POST"], defaults={"post_id": None})
@app.route("/admin/guia/<int:post_id>/editar", methods=["GET", "POST"])
@admin_required
def admin_guia_editar(post_id=None):
    atual = query_one("SELECT * FROM guia_posts WHERE id = %s", (post_id,)) if post_id else None
    if post_id and not atual:
        abort(404)
    if request.method == "GET":
        if atual:
            tags = query_all("SELECT t.nome FROM guia_post_tags pt JOIN guia_tags t ON t.id = pt.tag_id WHERE pt.post_id = %s ORDER BY t.nome",
                             (post_id,))
            return _guia_render_editor(atual, ", ".join(t["nome"] for t in tags))
        meu = query_one("SELECT id FROM guia_autores WHERE usuario_id = %s", (g.usuario["id"],)) or \
            query_one("SELECT id FROM guia_autores ORDER BY id LIMIT 1")
        return _guia_render_editor({"tipo": "artigo", "status": "rascunho", "corpo": "", "destaque": False, "noindex": False,
                                    "ordem_destaque": 0, "autor_id": meu["id"] if meu else None})
    d, tags, erros = _guia_ler_form(atual)
    arq = request.files.get("capa_arquivo")
    capa_url = (request.form.get("capa_url") or "").strip()
    if request.form.get("remover_capa") == "1":
        d["capa"] = None
    if capa_url and re.match(r"^https://", capa_url):
        d["capa"] = capa_url[:500]
    if not erros and arq and arq.filename:
        try:
            d["capa"] = storage.salvar_imagem(arq, arq.filename, "guia")
        except Exception as e:
            log.exception("capa do guia falhou")
            erros.append("Não consegui usar a imagem de capa: " + (str(e) if isinstance(e, ValueError) else "use PNG, JPG ou WEBP."))
    if erros:
        for e in erros:
            flash(e, "error")
        return _guia_render_editor(dict(atual or {}, **d, id=post_id), request.form.get("tags", ""))
    slug = guia_slug_livre("guia_posts", d["slug"], post_id)
    if slug != d["slug"]:
        flash(f"Já existia um post com esse endereço — usei “{slug}”.", "success")
    d["slug"] = slug
    if not d["autor_id"]:
        d["autor_id"] = guia_autor_padrao()
    if atual:
        execute("UPDATE guia_posts SET " + ", ".join(f"{c} = %({c})s" for c in _GUIA_CAMPOS) + ", atualizado_em = NOW() WHERE id = %(id)s",
                dict(d, id=post_id))
        if atual["slug"] != slug and atual["status"] in ("publicado", "agendado"):
            execute("INSERT INTO guia_redirecionamentos (slug_antigo, post_id) VALUES (%s, %s) "
                    "ON CONFLICT (slug_antigo) DO UPDATE SET post_id = EXCLUDED.post_id", (atual["slug"], post_id))
    else:
        post_id = execute_returning("INSERT INTO guia_posts (" + ", ".join(_GUIA_CAMPOS) + ") VALUES (" +
                                    ", ".join(f"%({c})s" for c in _GUIA_CAMPOS) + ") RETURNING id", d)
    execute("DELETE FROM guia_redirecionamentos WHERE slug_antigo = %s", (slug,))      # o endereço novo nunca redireciona
    execute("DELETE FROM guia_post_tags WHERE post_id = %s", (post_id,))
    for nome, s in tags:
        execute("INSERT INTO guia_tags (nome, slug) VALUES (%s, %s) ON CONFLICT (slug) DO NOTHING", (nome, s))
        execute("INSERT INTO guia_post_tags (post_id, tag_id) SELECT %s, id FROM guia_tags WHERE slug = %s ON CONFLICT DO NOTHING",
                (post_id, s))
    msg = {"publicado": "Post publicado.", "agendado": "Post agendado.", "rascunho": "Rascunho salvo."}[d["status"]]
    flash(msg, "success")
    return redirect(url_for("admin_guia_editar", post_id=post_id))


@app.route("/admin/guia/<int:post_id>/previa")
@admin_required
def admin_guia_previa(post_id):
    p = query_one(f"{GUIA_SEL_FULL} WHERE p.id = %s", (post_id,))
    if not p:
        abort(404)
    return _guia_render_post(p, previa=True)


@app.route("/admin/guia/<int:post_id>/<acao>", methods=["POST"])
@admin_required
def admin_guia_acao(post_id, acao):
    p = query_one("SELECT id, titulo FROM guia_posts WHERE id = %s", (post_id,))
    if not p or acao not in ("publicar", "despublicar", "destacar", "excluir"):
        abort(404)
    if acao == "publicar":
        execute("UPDATE guia_posts SET status = CASE WHEN COALESCE(publicado_em, NOW()) > NOW() THEN 'agendado' ELSE 'publicado' END, "
                "publicado_em = COALESCE(publicado_em, NOW()), atualizado_em = NOW() WHERE id = %s", (post_id,))
        flash("Post publicado.", "success")
    elif acao == "despublicar":
        execute("UPDATE guia_posts SET status = 'rascunho', atualizado_em = NOW() WHERE id = %s", (post_id,))
        flash("Post voltou para rascunho (saiu do ar).", "success")
    elif acao == "destacar":
        execute("UPDATE guia_posts SET destaque = NOT destaque WHERE id = %s", (post_id,))
        flash("Destaque atualizado.", "success")
    else:
        execute("DELETE FROM guia_posts WHERE id = %s", (post_id,))
        flash(f"“{p['titulo']}” foi excluído.", "success")
    return _admin_voltar("admin_guia")


@app.route("/admin/guia/imagem", methods=["POST"])
@admin_required
def admin_guia_imagem():
    """Upload de imagem dentro do editor (devolve a URL para inserir no texto)."""
    arq = request.files.get("arquivo")
    if not arq or not arq.filename:
        return jsonify(ok=False, erro="escolha um arquivo"), 400
    try:
        fid = storage.salvar_imagem(arq, arq.filename, "guia")
    except Exception as e:
        log.exception("imagem do guia falhou")
        return jsonify(ok=False, erro=str(e) if isinstance(e, ValueError) else "use PNG, JPG ou WEBP"), 400
    return jsonify(ok=True, url=url_foto(fid, 960), id=fid)


@app.route("/admin/guia/categorias")
@admin_required
def admin_guia_categorias():
    lista = query_all("SELECT c.*, (SELECT COUNT(*) FROM guia_posts p WHERE p.categoria_id = c.id) AS n FROM guia_categorias c "
                      "ORDER BY c.ordem, c.nome")
    return render_template("admin.html", pagina_ativa="guia_categorias", lista=lista)


@app.route("/admin/guia/categorias/salvar", methods=["POST"])
@admin_required
def admin_guia_categoria_salvar():
    f = request.form
    cid = _guia_int(f.get("id"))
    nome = sanitize_input(f.get("nome", ""))[:80]
    if not nome:
        flash("Dê um nome à categoria.", "error")
        return redirect(url_for("admin_guia_categorias"))
    dados = dict(nome=nome, descricao=sanitize_input(f.get("descricao", ""))[:400] or None,
                 seo_titulo=sanitize_input(f.get("seo_titulo", ""))[:70] or None,
                 seo_descricao=sanitize_input(f.get("seo_descricao", ""))[:200] or None,
                 ordem=_guia_int(f.get("ordem")) or 0, ativa=f.get("ativa") == "1")
    slug = guia_slug_livre("guia_categorias", _guia_s(f.get("slug")) or gerar_slug(nome), cid)
    if cid and query_one("SELECT 1 FROM guia_categorias WHERE id = %s", (cid,)):
        execute("UPDATE guia_categorias SET nome=%(nome)s, slug=%(slug)s, descricao=%(descricao)s, seo_titulo=%(seo_titulo)s, "
                "seo_descricao=%(seo_descricao)s, ordem=%(ordem)s, ativa=%(ativa)s WHERE id=%(id)s", dict(dados, slug=slug, id=cid))
        flash("Categoria salva.", "success")
    else:
        execute("INSERT INTO guia_categorias (nome, slug, descricao, seo_titulo, seo_descricao, ordem, ativa) "
                "VALUES (%(nome)s, %(slug)s, %(descricao)s, %(seo_titulo)s, %(seo_descricao)s, %(ordem)s, %(ativa)s)", dict(dados, slug=slug))
        flash("Categoria criada.", "success")
    return redirect(url_for("admin_guia_categorias"))


@app.route("/admin/guia/categorias/<int:cid>/excluir", methods=["POST"])
@admin_required
def admin_guia_categoria_excluir(cid):
    n = query_one("SELECT COUNT(*) AS n FROM guia_posts WHERE categoria_id = %s", (cid,))["n"]
    if n:
        flash(f"Essa categoria tem {n} post(s). Mude os posts de categoria antes de excluir (ou desative a categoria).", "error")
    else:
        execute("DELETE FROM guia_categorias WHERE id = %s", (cid,))
        flash("Categoria excluída.", "success")
    return redirect(url_for("admin_guia_categorias"))


@app.route("/admin/guia/autores")
@admin_required
def admin_guia_autores():
    lista = query_all("SELECT a.*, (SELECT COUNT(*) FROM guia_posts p WHERE p.autor_id = a.id) AS n FROM guia_autores a ORDER BY a.nome")
    for a in lista:
        a["foto_url"] = guia_capa_url(a["foto"], 120)
    return render_template("admin.html", pagina_ativa="guia_autores", lista=lista)


@app.route("/admin/guia/autores/salvar", methods=["POST"])
@admin_required
def admin_guia_autor_salvar():
    f = request.form
    aid = _guia_int(f.get("id"))
    nome = sanitize_input(f.get("nome", ""))[:80]
    if not nome:
        flash("Dê um nome ao autor.", "error")
        return redirect(url_for("admin_guia_autores"))
    atual = query_one("SELECT * FROM guia_autores WHERE id = %s", (aid,)) if aid else None
    foto = atual["foto"] if atual else None
    arq = request.files.get("foto_arquivo")
    if arq and arq.filename:
        try:
            foto = storage.salvar_imagem(arq, arq.filename, "guia")
        except Exception as e:
            flash("Não consegui usar a foto: " + (str(e) if isinstance(e, ValueError) else "use PNG, JPG ou WEBP."), "error")
            return redirect(url_for("admin_guia_autores"))
    dados = dict(nome=nome, cargo=sanitize_input(f.get("cargo", ""))[:80] or None, bio=sanitize_input(f.get("bio", ""))[:600] or None, foto=foto)
    slug = guia_slug_livre("guia_autores", _guia_s(f.get("slug")) or gerar_slug(nome), aid)
    if atual:
        execute("UPDATE guia_autores SET nome=%(nome)s, slug=%(slug)s, cargo=%(cargo)s, bio=%(bio)s, foto=%(foto)s WHERE id=%(id)s",
                dict(dados, slug=slug, id=aid))
        flash("Autor salvo.", "success")
    else:
        execute("INSERT INTO guia_autores (nome, slug, cargo, bio, foto) VALUES (%(nome)s, %(slug)s, %(cargo)s, %(bio)s, %(foto)s)",
                dict(dados, slug=slug))
        flash("Autor criado.", "success")
    return redirect(url_for("admin_guia_autores"))


@app.route("/admin/guia/autores/<int:aid>/excluir", methods=["POST"])
@admin_required
def admin_guia_autor_excluir(aid):
    n = query_one("SELECT COUNT(*) AS n FROM guia_posts WHERE autor_id = %s", (aid,))["n"]
    if n:
        flash(f"Esse autor tem {n} post(s). Troque o autor dos posts antes de excluir.", "error")
    else:
        execute("DELETE FROM guia_autores WHERE id = %s", (aid,))
        flash("Autor excluído.", "success")
    return redirect(url_for("admin_guia_autores"))


@app.route("/newsletter", methods=["POST"])
def newsletter():
    email = request.form.get("email", "").strip().lower()[:200]
    if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        execute("INSERT INTO newsletter (email) VALUES (%s) ON CONFLICT DO NOTHING", (email,))
        flash("Pronto! Você vai receber as novidades do OndeImóveis.", "success")
    else:
        flash("Informe um e-mail válido.", "error")
    return _admin_voltar("guia")          # volta para a página onde a pessoa estava (guia, post, categoria…)


@app.route("/robots.txt")
def robots():
    regras = ["User-agent: *", "Allow: /"] + [f"Disallow: {p}" for p in (
        "/painel", "/admin", "/minha-area", "/entrar", "/cadastrar", "/sair", "/favoritos", "/api/",
        "/imovel/*/whatsapp", "/imovel/*/visita", "/newsletter", "/webhooks/", "/guia/busca")]
    return Response("\n".join(regras) + f"\n\nSitemap: {base_url()}/sitemap.xml\n", mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    base = base_url()
    itens = [(base + "/", None)]
    for ep in ("buscar", "imobiliarias", "anuncie", "como_funciona", "guia", "para_empresas", "empresas_sites", "empresas_trafego", "mcmv.pagina"):
        if ep in app.view_functions:
            itens.append((base + url_for(ep), None))
    if "mcmv.cidade" in app.view_functions:      # páginas /minha-casa-minha-vida/<cidade> (só cidades com imóvel marcado)
        try:
            import mcmv
            for slug in mcmv.slugs_cidades():
                itens.append((base + url_for("mcmv.cidade", cidade=slug), None))
        except Exception:
            log.exception("MCMV: cidades fora do sitemap")
    # guia/blog: posts no ar (sem noindex), categorias e autores que têm post
    for r in query_all(f"SELECT p.slug, p.atualizado_em FROM guia_posts p WHERE {GUIA_PUB} AND NOT p.noindex "
                       "ORDER BY p.publicado_em DESC LIMIT 5000"):
        itens.append((base + url_for("guia_post", slug=r["slug"]), r["atualizado_em"]))
    for r in query_all(f"SELECT c.slug, MAX(p.atualizado_em) AS d FROM guia_categorias c JOIN guia_posts p ON p.categoria_id = c.id "
                       f"AND {GUIA_PUB} AND NOT p.noindex WHERE c.ativa GROUP BY c.slug"):
        itens.append((base + url_for("guia_categoria", slug=r["slug"]), r["d"]))
    for r in query_all(f"SELECT a.slug, MAX(p.atualizado_em) AS d FROM guia_autores a JOIN guia_posts p ON p.autor_id = a.id "
                       f"AND {GUIA_PUB} AND NOT p.noindex GROUP BY a.slug"):
        itens.append((base + url_for("guia_autor", slug=r["slug"]), r["d"]))
    for r in query_all("SELECT slug, atualizado_em FROM imoveis i WHERE status = 'publicado' AND EXISTS "
                       "(SELECT 1 FROM tenants t WHERE t.id = i.tenant_id AND t.status = 'ativo') ORDER BY id DESC LIMIT 5000"):
        itens.append((base + url_for("imovel", slug=r["slug"]), r["atualizado_em"]))
    for r in query_all("SELECT slug FROM tenants WHERE status = 'ativo' AND tipo = 'imobiliaria'"):
        itens.append((base + url_for("imobiliaria", slug=r["slug"]), None))
    # páginas de cidade e bairro (só as que têm imóvel)
    locais = set()
    for r in query_all("SELECT i.cidade_slug AS c, i.bairro_slug AS b, i.finalidade AS f, MAX(i.atualizado_em) AS d "
                       "FROM imoveis i JOIN tenants t ON t.id = i.tenant_id WHERE i.status = 'publicado' AND t.status = 'ativo' "
                       "GROUP BY i.cidade_slug, i.bairro_slug, i.finalidade"):
        for b in (None, r["b"]):
            for f_ in (None, r["f"]):
                if r["c"] and (b is None or r["b"]) and r["c"] == gerar_slug(r["c"]) and (b is None or b == gerar_slug(b)):
                    locais.add((r["c"], b, f_ if f_ in FINALIDADES else None))
    for c, b, f_ in sorted(locais, key=lambda x: (x[0], x[1] or "", x[2] or "")):
        itens.append((base + url_local(c, b, f_), None))
    corpo = "".join(f"<url><loc>{xml_escape(u)}</loc>" + (f"<lastmod>{d.date().isoformat()}</lastmod>" if d else "") + "</url>"
                    for u, d in itens)
    return Response('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"{corpo}</urlset>", mimetype="application/xml")


# ═══════════════════════════════════════════════════════════════
# 5. ÁREA DO VISITANTE E PAINEL DO ANUNCIANTE
# ═══════════════════════════════════════════════════════════════

@app.route("/minha-area")
@login_required
def minha_area():
    u = g.usuario
    cont = query_one(
        """SELECT (SELECT COUNT(*) FROM favoritos WHERE usuario_id = %(u)s) AS favoritos,
                  (SELECT COUNT(DISTINCT imovel_id) FROM eventos WHERE usuario_id = %(u)s AND tipo = 'view') AS visitados,
                  (SELECT COUNT(*) FROM eventos WHERE usuario_id = %(u)s AND tipo = 'whatsapp') AS contatos,
                  (SELECT COUNT(*) FROM eventos WHERE usuario_id = %(u)s AND tipo = 'visita') AS visitas""", {"u": u["id"]})
    continuar = query_all(
        f"""SELECT * FROM (SELECT DISTINCT ON (i.id) i.*, {CAPA_SQL} AS capa, e.criado_em AS visto_em FROM eventos e
              JOIN imoveis i ON i.id = e.imovel_id WHERE e.usuario_id = %s AND e.tipo = 'view' AND i.status = 'publicado'
              ORDER BY i.id, e.criado_em DESC) x ORDER BY visto_em DESC LIMIT 3""", (u["id"],))
    contatos = query_all(
        """SELECT e.criado_em, i.titulo, t.nome AS anunciante_nome FROM eventos e JOIN imoveis i ON i.id = e.imovel_id
             JOIN tenants t ON t.id = e.tenant_id WHERE e.usuario_id = %s AND e.tipo = 'whatsapp' ORDER BY e.criado_em DESC LIMIT 3""", (u["id"],))
    proxima = query_one(
        """SELECT e.detalhe, i.titulo, i.slug, t.nome AS anunciante_nome FROM eventos e JOIN imoveis i ON i.id = e.imovel_id
             JOIN tenants t ON t.id = e.tenant_id WHERE e.usuario_id = %s AND e.tipo = 'visita' ORDER BY e.criado_em DESC LIMIT 1""", (u["id"],))
    pct = 40 + 20 * bool(u["telefone"]) + 20 * bool(u["cidade"]) + 20 * bool(len(u["nome"].split()) > 1)
    return render_template("minha_area.html", st=cont, continuar=preparar_imoveis(continuar, 480), contatos=contatos,
                           proxima=proxima, perfil_pct=pct)


@app.route("/minha-area/perfil", methods=["GET", "POST"])
@login_required
def minha_area_perfil():
    if request.method == "POST":
        nome = sanitize_input(request.form.get("nome", ""))[:120]
        senha = request.form.get("senha", "")
        if len(nome) < 3:
            flash("Informe seu nome.", "error")
        elif senha and len(senha) < 8:
            flash("A nova senha precisa ter pelo menos 8 caracteres.", "error")
        else:
            execute("UPDATE usuarios SET nome = %s, telefone = %s, cidade = %s WHERE id = %s",
                    (nome, so_digitos(request.form.get("telefone")) or None,
                     sanitize_input(request.form.get("cidade", ""))[:80] or None, g.usuario["id"]))
            if senha:
                execute("UPDATE usuarios SET senha_hash = %s WHERE id = %s", (generate_password_hash(senha), g.usuario["id"]))
            flash("Perfil atualizado.", "success")
            return redirect(url_for("minha_area_perfil"))
    return render_template("minha_area_perfil.html")


def _periodo(t_id, dias):
    return query_one(
        """SELECT COUNT(*) FILTER (WHERE tipo = 'view') AS views,
                  COUNT(*) FILTER (WHERE tipo = 'view' AND criado_em >= NOW() - %s * INTERVAL '1 day') AS views_periodo,
                  COUNT(*) FILTER (WHERE tipo IN ('whatsapp','visita')) AS contatos,
                  COUNT(*) FILTER (WHERE tipo IN ('whatsapp','visita') AND criado_em >= NOW() - INTERVAL '30 days') AS contatos_30
             FROM eventos WHERE tenant_id = %s""", (dias, t_id))


def _ctx_geral():
    t = g.tenant
    dias = request.args.get("dias", 30, type=int)
    dias = dias if dias in (7, 30, 90) else 30
    est = query_one(
        """SELECT COUNT(*) FILTER (WHERE status = 'publicado') AS publicados,
                  COUNT(*) FILTER (WHERE status = 'publicado' AND criado_em >= NOW() - INTERVAL '30 days') AS novos_30,
                  COUNT(*) FILTER (WHERE status = 'pendente') AS pendentes,
                  COUNT(*) FILTER (WHERE status <> 'rejeitado') AS em_uso FROM imoveis WHERE tenant_id = %s""", (t["id"],))
    ev = _periodo(t["id"], dias)
    # barras: views agrupadas em N períodos
    n = 7 if dias == 7 else 10 if dias == 30 else 12
    passo = dias / n
    por_dia = {r["d"]: r["n"] for r in query_all(
        "SELECT (criado_em AT TIME ZONE %s)::date AS d, COUNT(*) AS n FROM eventos WHERE tenant_id = %s AND tipo = 'view' "
        "AND criado_em >= NOW() - %s * INTERVAL '1 day' GROUP BY 1", (str(TZ), t["id"], dias))}
    h = hoje()
    barras = []
    for k in range(n):
        fim = h - timedelta(days=round(dias - (k + 1) * passo))
        ini = h - timedelta(days=round(dias - k * passo) - 1)
        valor = sum(v for d, v in por_dia.items() if ini <= d <= fim)
        barras.append({"valor": valor, "label": fim.strftime("%d/%m"), "altura": 0})
    topo = max([b["valor"] for b in barras] + [1])
    for b in barras:
        b["altura"] = max(4, round(b["valor"] / topo * 100))
    leads = query_all(
        """SELECT e.criado_em, u.nome AS usuario_nome, i.titulo FROM eventos e JOIN imoveis i ON i.id = e.imovel_id
             LEFT JOIN usuarios u ON u.id = e.usuario_id WHERE e.tenant_id = %s AND e.tipo IN ('whatsapp','visita')
             ORDER BY e.criado_em DESC LIMIT 5""", (t["id"],))
    imoveis = query_all(
        f"""SELECT i.*, {CAPA_SQL} AS capa, (SELECT COUNT(*) FROM eventos e WHERE e.imovel_id = i.id AND e.tipo = 'view') AS views,
                   (SELECT COUNT(*) FROM eventos e WHERE e.imovel_id = i.id AND e.tipo IN ('whatsapp','visita')) AS contatos
              FROM imoveis i WHERE i.tenant_id = %s ORDER BY i.criado_em DESC LIMIT 5""", (t["id"],))
    for i in imoveis:
        i["foto"] = url_foto(i["capa"], 200)
    campos = [t["descricao"], t["creci"], t["horario"], t["whatsapp"], t["cidade"], t["instagram"] or t["facebook"] or t["linkedin"]]
    perfil_pct = round(sum(1 for c in campos if c) / len(campos) * 100)
    limite = t["limites"]["imoveis"]
    return dict(est=est, ev=ev, dias=dias, barras=barras, leads_resumo=leads, imoveis_resumo=imoveis,
                perfil_pct=perfil_pct, limite=limite, uso_pct=min(100, round(est["em_uso"] / limite * 100)))


def _ctx_imoveis():
    t = g.tenant
    lista = query_all(
        f"""SELECT i.*, {CAPA_SQL} AS capa, (SELECT COUNT(*) FROM eventos e WHERE e.imovel_id = i.id AND e.tipo = 'view') AS views,
                   (SELECT COUNT(*) FROM eventos e WHERE e.imovel_id = i.id AND e.tipo IN ('whatsapp','visita')) AS contatos
              FROM imoveis i WHERE i.tenant_id = %s ORDER BY i.criado_em DESC""", (t["id"],))
    for i in lista:
        i["foto"] = url_foto(i["capa"], 200)
    em_uso = query_one("SELECT COUNT(*) AS n FROM imoveis WHERE tenant_id = %s AND status <> 'rejeitado'", (t["id"],))["n"]
    destaques_usados = query_one("SELECT COUNT(*) AS n FROM imoveis WHERE tenant_id = %s AND destaque_anunciante AND status <> 'rejeitado'",
                                 (t["id"],))["n"]
    pausados_plano = sum(1 for i in lista if i["pausado_plano"] and i["status"] == "pausado")
    no_ar = sum(1 for i in lista if i["status"] in ("publicado", "pendente"))
    gratis_lotado = (not t["pro"]) and no_ar >= t["limites"]["imoveis"]      # mesma regra que anunciante_imovel_pausar usa pra recusar
    return dict(imoveis=lista, em_uso=em_uso, destaques_usados=destaques_usados,
                destaques_limite=LIMITES["profissional"]["destaques"], pausados_plano=pausados_plano, gratis_lotado=gratis_lotado)


def _ctx_interessados():
    leads = query_all(
        """SELECT e.tipo, e.detalhe, e.criado_em, u.nome AS usuario_nome, u.telefone AS usuario_telefone, i.titulo, i.slug
             FROM eventos e JOIN imoveis i ON i.id = e.imovel_id LEFT JOIN usuarios u ON u.id = e.usuario_id
            WHERE e.tenant_id = %s AND e.tipo IN ('whatsapp','visita') ORDER BY e.criado_em DESC LIMIT 200""", (g.tenant["id"],))
    return dict(leads=leads)


def _painel(ativa, **form):
    """Painel do anunciante: UMA página só (anunciante.html) com todas as seções.
    `ativa` = seção aberta ao carregar: geral | imoveis | form | interessados | pagina | assinatura."""
    t = g.tenant
    if not t["pro"]:
        sincronizar_planos(t["id"])                               # Pro venceu por data (sem evento): pausa o excedente já
    venc = t.get("plano_vencimento") if t["pro"] else None       # só o Pro ativo tem contagem regressiva
    dias_restantes = (venc - hoje()).days if venc else None
    pode_renovar = dias_restantes is not None and dias_restantes <= RENOVAR_ANTES_DIAS
    renovar_a_partir = (venc - timedelta(days=RENOVAR_ANTES_DIAS)) if venc and not pode_renovar else None
    ctx = dict(t=t, ativa=ativa, pro=t["pro"], pro_limites=LIMITES["profissional"], gratis_limites=LIMITES["gratis"],
               atrasado=t["plano"] != "gratis" and t["plano_efetivo"] == "gratis",
               i=None, val={}, sel=[], proximos=[], fotos=[],
               dias_restantes=dias_restantes, pode_renovar=pode_renovar, renovar_a_partir=renovar_a_partir,
               templates_pagina=metadados_templates_pagina(),
               template_atual=t.get("template") if t.get("template") in listar_templates_pagina() else TEMPLATE_PADRAO)
    ctx.update(
        recorrente_ativo=PAGBANK_RECORRENTE,
        planos_assinar=_planos_para_assinar(),
        assinatura_recorrente=query_one(
            "SELECT proxima_cobranca, vencimento, valor, meses FROM assinaturas WHERE tenant_id = %s AND recorrente AND status = 'ativa' "
            "AND vencimento >= %s ORDER BY id DESC LIMIT 1", (t["id"], hoje())),
        assinatura_atrasada=query_one(
            "SELECT 1 AS x FROM assinaturas a WHERE a.tenant_id = %s AND a.recorrente AND a.status = 'atrasada' AND NOT EXISTS "
            "(SELECT 1 FROM assinaturas b WHERE b.tenant_id = a.tenant_id AND b.status = 'ativa' AND b.id > a.id) LIMIT 1", (t["id"],)),
        assinatura_pendente=query_one(
            "SELECT 1 AS x FROM assinaturas WHERE tenant_id = %s AND status = 'pendente' "
            "AND criado_em > NOW() - INTERVAL '3 hours' LIMIT 1", (t["id"],)))
    ctx.update(_ctx_geral())
    ctx.update(_ctx_imoveis())
    ctx.update(_ctx_interessados())
    ctx.update(form)
    return render_template("anunciante.html", **ctx)


@app.route("/painel/geral")
@anunciante_required
def anunciante_visao_geral():
    return _painel("geral")


@app.route("/painel/imoveis")
@anunciante_required
def anunciante_imoveis():
    return _painel("imoveis")


def _meu_imovel(imovel_id):
    im = query_one("SELECT * FROM imoveis WHERE id = %s AND tenant_id = %s", (imovel_id, g.tenant["id"]))
    if not im:
        abort(404)
    return im


def _fotos_do_imovel(imovel_id):
    return [{"id": r["id"], "url": url_foto(r["file_id"], 320)} for r in
            query_all("SELECT id, file_id FROM imovel_fotos WHERE imovel_id = %s ORDER BY ordem, id", (imovel_id,))]


def _render_form_imovel(im, val, sel, proximos, fotos):
    proximos = [p if isinstance(p, dict) else
                {"categoria": p[0], "nome": p[1], "link": p[2], "distancia_m": p[3], "status": p[4]} for p in proximos]
    return _painel("form", i=im, val=val, sel=sel, proximos=proximos, fotos=fotos)


LINK_MAPS = re.compile(r"^https?://((www\.)?google\.[a-z.]+/maps|maps\.google\.[a-z.]+|maps\.app\.goo\.gl|goo\.gl/maps|www\.openstreetmap\.org)", re.I)


def _ler_form_imovel():
    f = request.form
    val, erros = {}, []
    val["titulo"] = sanitize_input(f.get("titulo", ""))[:140]
    val["finalidade"] = f.get("finalidade", "")
    val["tipo"] = f.get("tipo", "")
    val["preco"] = num_br(f.get("preco"))
    for k in ("area", "condominio", "iptu"):
        val[k] = num_br(f.get(k))
    for k in ("dormitorios", "suites", "banheiros", "vagas"):
        try:
            val[k] = max(0, min(99, int(f.get(k) or 0)))
        except ValueError:
            val[k] = 0
    val["cidade"] = sanitize_input(f.get("cidade", ""))[:80]
    val["uf"] = sanitize_input(f.get("uf", "")).upper()[:2] or None
    val["bairro"] = sanitize_input(f.get("bairro", ""))[:80]
    val["rua"] = _rua_sem_numero(sanitize_input(f.get("rua", ""))[:150]) or None   # só o nome da rua, nunca o número
    val["descricao"] = sanitize_input(f.get("descricao", ""))[:4000] or None
    val["whatsapp"] = so_digitos(f.get("whatsapp")) or None
    val["aceita_mcmv"] = f.get("aceita_mcmv") == "1" and val["finalidade"] == "venda"   # MCMV é financiamento de compra
    for k in ("lat", "lng"):
        v = num_br(f.get(k))
        val[k] = v
    sel = [c for c in f.getlist("caracteristicas") if c in CARACTERISTICAS]
    if len(val["titulo"]) < 8:
        erros.append("O título precisa ter pelo menos 8 caracteres.")
    if val["finalidade"] not in FINALIDADES:
        erros.append("Escolha a finalidade.")
    if val["tipo"] not in TIPOS_IMOVEL:
        erros.append("Escolha o tipo do imóvel.")
    if not val["preco"] or val["preco"] <= 0:
        erros.append("Informe o preço.")
    if not val["cidade"] or not val["bairro"]:
        erros.append("Informe cidade e bairro.")
    if (val["lat"] is None) != (val["lng"] is None) or (val["lat"] is not None and not (-90 <= val["lat"] <= 90 and -180 <= val["lng"] <= 180)):
        erros.append("Latitude e longitude inválidas.")
    prox = []
    for cat, nome, link, dist in list(zip(f.getlist("prox_categoria"), f.getlist("prox_nome"), f.getlist("prox_link"), f.getlist("prox_dist")))[:10]:
        nome = sanitize_input(nome)[:80]
        if not nome or cat not in CATEGORIAS_PROXIMO:
            continue
        link = link.strip()[:500]
        ok = bool(link) and LINK_MAPS.match(link) is not None
        d = int(dist) if dist.strip().isdigit() else None
        prox.append((cat, nome, link if ok else None, d, "aprovado" if ok else "pendente"))
    return val, sel, prox, erros


def _salvar_fotos(imovel_id, t):
    arquivos = [a for a in request.files.getlist("fotos") if a and a.filename]
    atuais = query_one("SELECT COUNT(*) AS n, COALESCE(MAX(ordem), -1) AS m FROM imovel_fotos WHERE imovel_id = %s", (imovel_id,))
    vagas = max(t["limites"]["fotos"] - atuais["n"], 0)     # nunca negativo (imóvel acima do teto novo mantém as fotos)
    if len(arquivos) > vagas:
        if vagas == 0:
            flash(f"Este imóvel já está no limite de {t['limites']['fotos']} fotos do seu plano; nenhuma foto nova foi adicionada.", "error")
        else:
            flash(f"Seu plano permite {t['limites']['fotos']} fotos por imóvel; enviei só as primeiras {vagas}.", "error")
        arquivos = arquivos[:vagas]
    ordem = atuais["m"] + 1
    for a in arquivos:
        try:
            fid = storage.salvar_imagem(a.stream, a.filename, "imoveis")
        except Exception:
            flash(f"Não consegui ler “{sanitize_input(a.filename)[:40]}” (use JPG, PNG ou WebP).", "error")
            continue
        execute("INSERT INTO imovel_fotos (imovel_id, file_id, ordem) VALUES (%s,%s,%s)", (imovel_id, fid, ordem))
        ordem += 1


def _gravar_proximos(imovel_id, prox):
    execute("DELETE FROM imovel_proximos WHERE imovel_id = %s", (imovel_id,))
    for p in prox:
        execute("INSERT INTO imovel_proximos (imovel_id, categoria, nome, link, distancia_m, status) VALUES (%s,%s,%s,%s,%s,%s)",
                (imovel_id, *p))


@app.route("/painel/imoveis/novo", methods=["GET", "POST"])
@anunciante_required
def anunciante_imovel_novo():
    t = g.tenant
    em_uso = query_one("SELECT COUNT(*) AS n FROM imoveis WHERE tenant_id = %s AND status <> 'rejeitado'", (t["id"],))["n"]
    if em_uso >= t["limites"]["imoveis"]:
        flash(f"Você atingiu o limite de {t['limites']['imoveis']} imóveis do seu plano.", "error")
        return redirect(url_for("anunciante_assinatura"))
    if request.method == "POST":
        val, sel, prox, erros = _ler_form_imovel()
        if erros:
            for e in erros:
                flash(e, "error")
            return _render_form_imovel(None, val, sel, prox, [])
        iid = execute_returning(
            "INSERT INTO imoveis (tenant_id, titulo, slug, finalidade, tipo, preco, condominio, iptu, cidade, cidade_slug, uf, bairro, "
            "bairro_slug, dormitorios, suites, banheiros, vagas, area, descricao, caracteristicas, whatsapp, lat, lng, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pendente') RETURNING id",
            (t["id"], val["titulo"], slug_unico("imoveis", gerar_slug(val["titulo"])), val["finalidade"], val["tipo"], val["preco"],
             val["condominio"], val["iptu"], val["cidade"], gerar_slug(val["cidade"]), val["uf"], val["bairro"], gerar_slug(val["bairro"]),
             val["dormitorios"], val["suites"], val["banheiros"], val["vagas"], val["area"], val["descricao"], "|".join(sel),
             val["whatsapp"], val["lat"], val["lng"]))
        execute("UPDATE imoveis SET rua = %s, aceita_mcmv = %s WHERE id = %s", (val["rua"], val["aceita_mcmv"], iid))
        _gravar_proximos(iid, prox)
        _salvar_fotos(iid, t)
        avisar_novo_anuncio(iid)
        flash("Imóvel enviado! Ele entra no ar assim que for aprovado.", "success")
        return redirect(url_for("anunciante_imoveis"))
    return _render_form_imovel(None, {}, [], [], [])


@app.route("/painel/imoveis/<int:imovel_id>/editar", methods=["GET", "POST"])
@anunciante_required
def anunciante_imovel_editar(imovel_id):
    t, im = g.tenant, _meu_imovel(imovel_id)
    if request.method == "POST":
        val, sel, prox, erros = _ler_form_imovel()
        if erros:
            for e in erros:
                flash(e, "error")
            return _render_form_imovel(im, val, sel, prox, _fotos_do_imovel(imovel_id))
        # Qualquer edição volta o anúncio para análise (evita aprovar um anúncio limpo e trocar o conteúdo depois).
        status = "pendente"
        execute(
            "UPDATE imoveis SET titulo=%s, finalidade=%s, tipo=%s, preco=%s, condominio=%s, iptu=%s, cidade=%s, cidade_slug=%s, uf=%s, "
            "bairro=%s, bairro_slug=%s, dormitorios=%s, suites=%s, banheiros=%s, vagas=%s, area=%s, descricao=%s, caracteristicas=%s, "
            "whatsapp=%s, lat=%s, lng=%s, status=%s, motivo_rejeicao=NULL, atualizado_em=NOW() WHERE id=%s",
            (val["titulo"], val["finalidade"], val["tipo"], val["preco"], val["condominio"], val["iptu"], val["cidade"],
             gerar_slug(val["cidade"]), val["uf"], val["bairro"], gerar_slug(val["bairro"]), val["dormitorios"], val["suites"],
             val["banheiros"], val["vagas"], val["area"], val["descricao"], "|".join(sel), val["whatsapp"], val["lat"], val["lng"],
             status, imovel_id))
        execute("UPDATE imoveis SET rua = %s, aceita_mcmv = %s WHERE id = %s", (val["rua"], val["aceita_mcmv"], imovel_id))
        _gravar_proximos(imovel_id, prox)
        _salvar_fotos(imovel_id, t)
        if im["status"] != "pendente":      # já estava na fila? não manda aviso repetido
            avisar_novo_anuncio(imovel_id, editado=True)
        flash("Alterações salvas. O anúncio voltou para análise e só reaparece no site depois de aprovado.", "success")
        return redirect(url_for("anunciante_imovel_editar", imovel_id=imovel_id))
    proximos = query_all("SELECT * FROM imovel_proximos WHERE imovel_id = %s ORDER BY id", (imovel_id,))
    return _render_form_imovel(im, im, [c for c in (im["caracteristicas"] or "").split("|") if c], proximos, _fotos_do_imovel(imovel_id))


@app.route("/painel/imoveis/<int:imovel_id>/pausar", methods=["POST"])
@anunciante_required
def anunciante_imovel_pausar(imovel_id):
    im = _meu_imovel(imovel_id)
    novo = {"publicado": "pausado", "pausado": "publicado"}.get(im["status"])
    if novo == "publicado" and not g.tenant["pro"]:        # Grátis: reativar só se couber nos imóveis no ar
        no_ar = query_one("SELECT COUNT(*) AS n FROM imoveis WHERE tenant_id = %s AND status IN ('publicado', 'pendente')",
                          (g.tenant["id"],))["n"]
        if no_ar >= g.tenant["limites"]["imoveis"]:
            flash(f"No plano Grátis só {g.tenant['limites']['imoveis']} imóveis ficam no ar. Pause outro antes ou assine o Profissional.", "error")
            return redirect(url_for("anunciante_imoveis"))
    if novo:
        execute("UPDATE imoveis SET status = %s, pausado_plano = FALSE, atualizado_em = NOW() WHERE id = %s", (novo, imovel_id))
    return redirect(url_for("anunciante_imoveis"))


@app.route("/painel/imoveis/<int:imovel_id>/destaque", methods=["POST"])
@anunciante_required
def anunciante_imovel_destaque(imovel_id):
    """Liga/desliga o destaque do próprio anunciante (Pro, até LIMITES['profissional']['destaques']). O destaque do admin é outro campo."""
    t = g.tenant
    im = _meu_imovel(imovel_id)                       # 404 se o imóvel não for dele
    if im["destaque_anunciante"]:                     # desligar é sempre permitido (inclusive com o plano vencido)
        execute("UPDATE imoveis SET destaque_anunciante = FALSE WHERE id = %s AND tenant_id = %s", (imovel_id, t["id"]))
        flash("Destaque removido.", "success")
        return redirect(url_for("anunciante_imoveis"))
    if not t["pro"]:
        flash("Imóvel em destaque é um recurso do plano Profissional.", "error")
        return redirect(url_for("anunciante_assinatura"))
    limite = LIMITES["profissional"]["destaques"]
    if im["status"] == "rejeitado":
        flash("Imóveis rejeitados não podem ser destacados.", "error")
        return redirect(url_for("anunciante_imoveis"))
    # UPDATE único com a contagem dentro: dois cliques ao mesmo tempo não passam do limite
    ok = execute("UPDATE imoveis SET destaque_anunciante = TRUE WHERE id = %s AND tenant_id = %s AND "
                 "(SELECT COUNT(*) FROM imoveis WHERE tenant_id = %s AND destaque_anunciante AND status <> 'rejeitado') < %s",
                 (imovel_id, t["id"], t["id"], limite))
    if ok:
        flash("Imóvel colocado em destaque.", "success")
    else:
        flash(f"Você já usou os {limite} destaques do seu plano. Remova um para destacar outro.", "error")
    return redirect(url_for("anunciante_imoveis"))


@app.route("/painel/imoveis/<int:imovel_id>/excluir", methods=["POST"])
@anunciante_required
def anunciante_imovel_excluir(imovel_id):
    _meu_imovel(imovel_id)
    arquivos = [f["file_id"] for f in query_all("SELECT file_id FROM imovel_fotos WHERE imovel_id = %s", (imovel_id,))]
    execute("DELETE FROM imoveis WHERE id = %s", (imovel_id,))
    for fid in arquivos:
        storage.deletar_imagem(fid)
    flash("Imóvel excluído.", "success")
    return redirect(url_for("anunciante_imoveis"))


@app.route("/painel/imoveis/<int:imovel_id>/fotos/<int:foto_id>/<acao>", methods=["POST"])
@anunciante_required
def anunciante_foto_acao(imovel_id, foto_id, acao):
    _meu_imovel(imovel_id)
    foto = query_one("SELECT * FROM imovel_fotos WHERE id = %s AND imovel_id = %s", (foto_id, imovel_id))
    if not foto:
        abort(404)
    if acao == "capa":
        menor = query_one("SELECT COALESCE(MIN(ordem), 0) AS m FROM imovel_fotos WHERE imovel_id = %s", (imovel_id,))["m"]
        execute("UPDATE imovel_fotos SET ordem = %s WHERE id = %s", (menor - 1, foto_id))
    elif acao == "excluir":
        execute("DELETE FROM imovel_fotos WHERE id = %s", (foto_id,))
        storage.deletar_imagem(foto["file_id"])
    else:
        abort(404)
    return redirect(url_for("anunciante_imovel_editar", imovel_id=imovel_id))


@app.route("/painel/interessados")
@anunciante_required
def anunciante_interessados():
    return _painel("interessados")


@app.route("/painel/pagina", methods=["GET", "POST"])
@anunciante_required
def anunciante_pagina():
    t = g.tenant
    if request.method == "POST":
        f = request.form
        nome = sanitize_input(f.get("nome", ""))[:120]
        if len(nome) < 3:
            flash("Informe o nome da página.", "error")
            return redirect(url_for("anunciante_pagina"))
        anos = f.get("anos_mercado", "")
        cor = f.get("cor_primaria", "")
        logo_id = t["logo_file_id"]
        if t["pro"]:
            arq = request.files.get("logo")
            if arq and arq.filename:
                try:
                    novo = storage.salvar_imagem(arq.stream, arq.filename, "logos")
                    if logo_id:
                        storage.deletar_imagem(logo_id)
                    logo_id = novo
                except Exception:
                    flash("Não consegui ler a logo (use JPG, PNG ou WebP).", "error")
        else:
            cor = None
        # Template: só Pro grava, e só valores que existem na pasta. Não-Pro mantém o que já estava gravado.
        template = t.get("template")
        if t["pro"]:
            escolhido = f.get("template", "")
            if escolhido in listar_templates_pagina():
                template = escolhido
        execute(
            "UPDATE tenants SET nome=%s, creci=%s, anos_mercado=%s, descricao=%s, telefone=%s, whatsapp=%s, horario=%s, cidade=%s, uf=%s, "
            "instagram=%s, facebook=%s, linkedin=%s, logo_file_id=%s, cor_primaria=%s, template=%s WHERE id=%s",
            (nome, sanitize_input(f.get("creci", ""))[:20] or None, int(anos) if anos.isdigit() else None,
             sanitize_input(f.get("descricao", ""))[:300] or None, so_digitos(f.get("telefone")) or None,
             so_digitos(f.get("whatsapp")) or None, sanitize_input(f.get("horario", ""))[:80] or None,
             sanitize_input(f.get("cidade", ""))[:80] or None, sanitize_input(f.get("uf", "")).upper()[:2] or None,
             sanitize_input(f.get("instagram", ""))[:120] or None, sanitize_input(f.get("facebook", ""))[:120] or None,
             sanitize_input(f.get("linkedin", ""))[:120] or None, logo_id,
             cor if cor and re.fullmatch(r"#[0-9a-fA-F]{6}", cor) else None, template, t["id"]))
        flash("Página atualizada.", "success")
        return redirect(url_for("anunciante_pagina"))
    return _painel("pagina")


@app.route("/painel/assinatura")
@anunciante_required
def anunciante_assinatura():
    return _painel("assinatura")



@app.route("/minha-area/<aba>")
@login_required
def minha_area_lista(aba):
    """Imóveis visitados · Meus contatos · Visitas agendadas."""
    if aba not in ("visitados", "contatos", "visitas"):
        abort(404)
    u = g.usuario
    if aba == "visitados":
        itens = query_all(
            f"""SELECT * FROM (SELECT DISTINCT ON (i.id) i.*, t.nome AS anunciante_nome, {CAPA_SQL} AS capa, e.criado_em AS visto_em
                  FROM eventos e JOIN imoveis i ON i.id = e.imovel_id JOIN tenants t ON t.id = i.tenant_id
                 WHERE e.usuario_id = %s AND e.tipo = 'view' AND i.status = 'publicado'
                 ORDER BY i.id, e.criado_em DESC) x ORDER BY visto_em DESC LIMIT 60""", (u["id"],))
        preparar_imoveis(itens, 480)
    else:
        itens = query_all(
            """SELECT e.tipo, e.detalhe, e.criado_em, i.titulo, i.slug, t.nome AS anunciante_nome FROM eventos e
                 JOIN imoveis i ON i.id = e.imovel_id JOIN tenants t ON t.id = e.tenant_id
                WHERE e.usuario_id = %s AND e.tipo = %s ORDER BY e.criado_em DESC LIMIT 100""",
            (u["id"], "whatsapp" if aba == "contatos" else "visita"))
    titulos = {"visitados": "Imóveis visitados", "contatos": "Meus contatos", "visitas": "Visitas agendadas"}
    return render_template("minha_area_lista.html", aba=aba, itens=itens, titulo=titulos[aba])


# ═══════════════════════════════════════════════════════════════
# 6. PAGAMENTOS — regra de negócio separada do provedor (PagBank)
# ═══════════════════════════════════════════════════════════════

class pagbank:
    """Adaptador do PagBank (Checkout). Para trocar de provedor, crie outra classe com os mesmos 2 métodos."""
    nome = "pagbank"

    @staticmethod
    def configurado():
        return bool(PAGBANK_TOKEN)

    @staticmethod
    def criar_cobranca(tenant, assinatura_id, valor, recorrente=False, meses=1, nome_plano="Mensal"):
        """Cria o checkout. recorrente=True → Checkout Recorrente (só cartão): a 1ª cobrança é na hora e o PagBank cobra as
        seguintes sozinho. Devolve (id_do_checkout, link_de_pagamento, id_do_plano_recorrente | None)."""
        host = "https://sandbox.api.pagseguro.com" if PAGBANK_SANDBOX else "https://api.pagseguro.com"
        base = BASE_URL or request.host_url.rstrip("/")
        corpo = {
            "reference_id": f"assin-{assinatura_id}",
            "items": [{"name": f"OndeImóveis — Plano Profissional ({nome_plano})",
                       "quantity": 1, "unit_amount": int(round(valor * 100))}],
            "redirect_url": base + url_for("anunciante_assinatura"),
            "payment_notification_urls": [base + url_for("webhook_pagbank") + (f"?t={quote(PAGBANK_WEBHOOK_TOKEN)}" if PAGBANK_WEBHOOK_TOKEN else "")],
        }
        if recorrente:      # sem billing_cycles = não expira (cobra a cada `meses` meses até cancelar)
            corpo["recurrence_plan"] = {"name": f"OndeImóveis — Plano Profissional {nome_plano}", "interval": {"unit": "MONTH", "length": meses}}
        r = requests.post(f"{host}/checkouts", json=corpo, timeout=20,
                          headers={"Authorization": f"Bearer {PAGBANK_TOKEN}", "Content-Type": "application/json"})
        r.raise_for_status()
        dados = r.json()
        link = next((l["href"] for l in dados.get("links", []) if l.get("rel") == "PAY"), None)
        if not link:
            raise RuntimeError("PagBank não devolveu o link de pagamento.")
        return dados.get("id"), link, (dados.get("recurrence_plan") or {}).get("id")

    @staticmethod
    def consultar_pedido(order_id):
        """Busca o pedido direto na API do PagBank (fonte da verdade). Levanta exceção se a API falhar."""
        host = "https://sandbox.api.pagseguro.com" if PAGBANK_SANDBOX else "https://api.pagseguro.com"
        r = requests.get(f"{host}/orders/{quote(str(order_id), safe='')}", timeout=15,
                         headers={"Authorization": f"Bearer {PAGBANK_TOKEN}", "Accept": "application/json"})
        r.raise_for_status()
        return r.json()

    @staticmethod
    def pagamento_confere(order_id, assinatura):
        """True só se o PagBank confirma: pedido da nossa referência, cobrança paga e valor igual ao da assinatura."""
        o = pagbank.consultar_pedido(order_id)
        if o.get("reference_id") != f"assin-{assinatura['id']}":
            return False
        esperado = int(round(float(assinatura["valor"]) * 100))
        return any(str(c.get("status", "")).upper() in ("PAID", "AUTHORIZED") and (c.get("amount") or {}).get("value") == esperado
                   for c in (o.get("charges") or []))

    @staticmethod
    def autentico(corpo_bruto):
        """Valida o webhook: assinatura x-authenticity-token (sha256 de 'token-corpo') ou ?t=<token do .env>."""
        enviado = request.headers.get("x-authenticity-token", "")
        if PAGBANK_TOKEN and enviado:
            esperado = hashlib.sha256(f"{PAGBANK_TOKEN}-{corpo_bruto.decode('utf-8', 'ignore')}".encode()).hexdigest()
            if hmac.compare_digest(enviado, esperado):
                return True
        t = request.args.get("t", "")
        return bool(PAGBANK_WEBHOOK_TOKEN) and hmac.compare_digest(t, PAGBANK_WEBHOOK_TOKEN)

    @staticmethod
    def interpretar(dados):
        """Normaliza o payload → {evento_id, referencia, status: pago|pendente|falhou, valor}."""
        cobrancas = dados.get("charges") or []
        referencia = dados.get("reference_id") or (cobrancas[0].get("reference_id") if cobrancas else None)
        status_bruto = (cobrancas[0].get("status") if cobrancas else dados.get("status") or "").upper()
        status = {"PAID": "pago", "AUTHORIZED": "pago", "DECLINED": "falhou", "CANCELED": "falhou",
                  "WAITING": "pendente", "IN_ANALYSIS": "pendente"}.get(status_bruto, "pendente")
        valor = None
        if cobrancas and cobrancas[0].get("amount", {}).get("value") is not None:
            valor = cobrancas[0]["amount"]["value"] / 100
        evento_id = (cobrancas[0].get("id") if cobrancas else None) or dados.get("id") or hashlib.sha1(
            json.dumps(dados, sort_keys=True).encode()).hexdigest()
        marca = "PAID" if status == "pago" else status_bruto            # AUTHORIZED e PAID da mesma cobrança = 1 evento só
        return {"evento_id": f"{evento_id}:{marca}", "referencia": referencia, "status": status, "valor": valor}

    @staticmethod
    def interpretar_assinatura(dados):
        """Aviso de assinatura (subscription.*) → {evento, assinatura_id, plano_id, reference_id, status, proxima, atualizado, valor}."""
        r = dados.get("resource") or {}
        try:
            proxima = date.fromisoformat(str(r.get("next_invoice_at") or "")[:10])
        except ValueError:
            proxima = None
        valor = (r.get("amount") or {}).get("value")
        return {"evento": str(dados.get("event") or ""), "assinatura_id": r.get("id"), "plano_id": (r.get("plan") or {}).get("id"),
                "reference_id": r.get("reference_id"), "status": str(r.get("status") or "").upper(), "proxima": proxima,
                "atualizado": str(r.get("updated_at") or r.get("created_at") or ""),
                "valor": valor / 100 if isinstance(valor, (int, float)) else None}


def sincronizar_planos(tenant_id=None):
    """Põe os imóveis de acordo com o plano. Sem tenant_id, vale para todos (cron).
    • Grátis (inclusive Pro vencido): só os N mais recentes ficam publicados (N = limite do Grátis); o excedente vira 'pausado' com
      pausado_plano = TRUE. Nada é apagado.
    • Pro ativo: os pausados por plano voltam a 'publicado'. Os que o próprio anunciante pausou não mexem.
    Idempotente: pode rodar quantas vezes quiser. Devolve (pausados, reativados)."""
    filtro, p = ("AND t.id = %s", (tenant_id,)) if tenant_id else ("", ())
    pro = f"COALESCE({sql_pro_ativo('t')}, FALSE)"
    reativados = execute(f"UPDATE imoveis i SET status = 'publicado', pausado_plano = FALSE, atualizado_em = NOW() FROM tenants t "
                         f"WHERE t.id = i.tenant_id AND i.pausado_plano AND i.status = 'pausado' AND {pro} {filtro}", p)
    pausados = execute(
        f"UPDATE imoveis SET status = 'pausado', pausado_plano = TRUE, atualizado_em = NOW() WHERE id IN ("
        f"SELECT id FROM (SELECT i.id, ROW_NUMBER() OVER (PARTITION BY i.tenant_id ORDER BY i.criado_em DESC, i.id DESC) AS rn "
        f"FROM imoveis i JOIN tenants t ON t.id = i.tenant_id WHERE i.status = 'publicado' AND NOT {pro} {filtro}) x WHERE rn > %s)",
        p + (LIMITES["gratis"]["imoveis"],))
    execute("UPDATE imoveis SET pausado_plano = FALSE WHERE pausado_plano AND status <> 'pausado'")   # limpa marca de quem mudou de status
    return pausados, reativados


def _somar_meses(d, n):
    """Soma n meses de calendário a uma data (31/01 + 1 mês = 28/02, nunca 03/03)."""
    m = d.month - 1 + n
    ano, mes = d.year + m // 12, m % 12 + 1
    return d.replace(year=ano, month=mes, day=min(d.day, calendar.monthrange(ano, mes)[1]))


def planos_pro(so_ativos=True):
    """Períodos do Profissional (tabela planos_pro, editável em /admin/planos). Uma consulta por requisição."""
    if "planos_pro" not in g:
        g.planos_pro = [dict(r, valor=float(r["valor"])) for r in query_all("SELECT * FROM planos_pro ORDER BY meses")]
    return [p for p in g.planos_pro if p["ativo"]] if so_ativos else list(g.planos_pro)


def plano_pro(codigo, so_ativo=True):
    return next((p for p in planos_pro(so_ativo) if p["codigo"] == codigo), None)


def preco_mensal():
    """Preço do plano mensal (o que aparece como 'R$ x /mês'). Se a tabela falhar, cai no PRECO_PROFISSIONAL do .env."""
    try:
        p = plano_pro("mensal", so_ativo=False)
        return p["valor"] if p else PRECO_PROFISSIONAL
    except Exception:
        return PRECO_PROFISSIONAL


def _planos_para_assinar():
    """Períodos ativos prontos pro painel: preço, valor por mês e % de economia sobre o mensal."""
    base = preco_mensal()
    return [dict(p, por_mes=p["valor"] / p["meses"],
                 economia=max(0, round((1 - p["valor"] / (base * p["meses"])) * 100)) if p["meses"] > 1 and base else 0)
            for p in planos_pro()]


def ativar_profissional(tenant_id, dias=30, assinatura_id=None, provedor=None, meses=None, valor=None, plano_codigo=None):
    """Regra de negócio: ativa/renova o Profissional. Chamada pelo webhook e pelo admin.
    `meses` (planos trimestral/semestral/anual) soma meses de calendário; sem ele vale `dias` (30), como antes."""
    t = query_one("SELECT plano, plano_vencimento FROM tenants WHERE id = %s", (tenant_id,))
    inicio = max(hoje(), t["plano_vencimento"]) if (t["plano_vencimento"] and t["plano"] == "profissional") else hoje()
    venc = _somar_meses(inicio, meses) if meses else inicio + timedelta(days=dias)
    execute("UPDATE tenants SET plano = 'profissional', assinatura_status = 'ativa', plano_vencimento = %s WHERE id = %s",
            (venc, tenant_id))
    if assinatura_id:
        execute("UPDATE assinaturas SET status = 'ativa', vencimento = %s, atualizado_em = NOW() WHERE id = %s",
                (venc, assinatura_id))
    else:
        execute("INSERT INTO assinaturas (tenant_id, plano, status, valor, vencimento, provedor_pagamento, plano_codigo, meses) "
                "VALUES (%s,'profissional','ativa',%s,%s,%s,%s,%s)",
                (tenant_id, valor if valor is not None else preco_mensal(), venc, provedor or "manual", plano_codigo, meses))
    sincronizar_planos(tenant_id)                      # reativa os imóveis que estavam pausados por falta de plano
    return venc


def renovar_ate(tenant_id, ate, assinatura_id=None, proxima=None):
    """Recorrência: o Pro vale até a data paga (próxima cobrança + folga). Só avança, nunca encurta. Idempotente:
    receber o mesmo aviso duas vezes (ou fora de ordem) dá o mesmo resultado."""
    if ate < hoje():
        return None
    t = query_one("SELECT plano, assinatura_status, plano_vencimento FROM tenants WHERE id = %s", (tenant_id,))
    atual = t["plano_vencimento"] if t["plano"] == "profissional" and t["assinatura_status"] == "ativa" else None
    venc = max(ate, atual) if atual else ate
    execute("UPDATE tenants SET plano = 'profissional', assinatura_status = 'ativa', plano_vencimento = %s WHERE id = %s", (venc, tenant_id))
    if assinatura_id:
        execute("UPDATE assinaturas SET status = 'ativa', vencimento = %s, proxima_cobranca = %s, atualizado_em = NOW() WHERE id = %s",
                (venc, proxima, assinatura_id))
    sincronizar_planos(tenant_id)
    return venc


def desativar_profissional(tenant_id, status="cancelada"):
    execute("UPDATE tenants SET assinatura_status = %s WHERE id = %s", (status, tenant_id))
    execute("UPDATE assinaturas SET status = %s, atualizado_em = NOW() WHERE tenant_id = %s AND status = 'ativa'",
            (status, tenant_id))
    sincronizar_planos(tenant_id)                      # voltou ao Grátis: o excedente é pausado agora


@app.route("/painel/assinatura/assinar", methods=["POST"])
@anunciante_required
def anunciante_assinar():
    t = g.tenant
    if query_one("SELECT 1 FROM assinaturas WHERE tenant_id = %s AND recorrente AND status = 'ativa' AND vencimento >= %s",
                 (t["id"], hoje())):
        flash("Sua assinatura renova sozinha no cartão. Para cancelar a renovação automática, fale com o suporte.", "success")
        return redirect(url_for("anunciante_assinatura"))
    if t["plano_efetivo"] == "profissional":
        # Pro ativo: só abre cobrança de renovação perto do vencimento (o webhook soma 30 dias ao vencimento atual)
        venc = t.get("plano_vencimento")
        if not venc:
            flash("Você já está no plano Profissional.", "success")
            return redirect(url_for("anunciante_assinatura"))
        if (venc - hoje()).days > RENOVAR_ANTES_DIAS:
            flash(f"Você poderá renovar a partir de {(venc - timedelta(days=RENOVAR_ANTES_DIAS)).strftime('%d/%m')}.", "success")
            return redirect(url_for("anunciante_assinatura"))
    if not pagbank.configurado():
        flash("O pagamento online ainda não está ativo. Fale com o suporte para contratar o Profissional.", "error")
        return redirect(url_for("anunciante_assinatura"))
    # modo: "recorrente" (cartão, renova sozinho) ou "avulso" (período escolhido, pago uma vez; Pix, boleto ou cartão). Sem o campo no formulário vale o padrão
    # do servidor (PAGBANK_RECORRENTE).
    modo = (request.form.get("modo") or ("recorrente" if PAGBANK_RECORRENTE else "avulso")).lower()
    recorrente = modo == "recorrente" and PAGBANK_RECORRENTE
    plano = plano_pro(request.form.get("plano") or "mensal")           # só períodos ativos; sem o campo vale o mensal
    if not plano:
        flash("Escolha um dos planos disponíveis.", "error")
        return redirect(url_for("anunciante_assinatura"))
    aid = execute_returning("INSERT INTO assinaturas (tenant_id, plano, status, valor, provedor_pagamento, recorrente, plano_codigo, meses) "
                            "VALUES (%s,'profissional','pendente',%s,'pagbank',%s,%s,%s) RETURNING id",
                            (t["id"], plano["valor"], recorrente, plano["codigo"], plano["meses"]))
    try:
        try:
            ref, link, plano_ext = pagbank.criar_cobranca(t, aid, plano["valor"], recorrente=recorrente, meses=plano["meses"], nome_plano=plano["nome"])
        except Exception:
            if not recorrente:
                raise
            log.exception("PagBank recusou o checkout recorrente; abrindo cobrança avulsa no lugar")
            recorrente = False
            execute("UPDATE assinaturas SET recorrente = FALSE WHERE id = %s", (aid,))
            ref, link, plano_ext = pagbank.criar_cobranca(t, aid, plano["valor"], recorrente=False, meses=plano["meses"], nome_plano=plano["nome"])
    except Exception:
        log.exception("Falha ao criar cobrança no PagBank")
        flash("Não consegui abrir o pagamento agora. Tente de novo em instantes.", "error")
        return redirect(url_for("anunciante_assinatura"))
    execute("UPDATE assinaturas SET referencia_externa = %s, plano_externo_id = %s WHERE id = %s", (ref, plano_ext, aid))
    return redirect(link)


def _email_renovacao(tipo, nome, venc, dias, link):
    """Assunto, texto e HTML do aviso de vencimento do Pro."""
    data = venc.strftime("%d/%m/%Y")
    if tipo == "vencido":
        assunto = "Seu plano Profissional venceu"
        abre = f"O plano Profissional de {nome} venceu em {data}."
        fecha = "Você pode reativar quando quiser: ao renovar, os imóveis pausados voltam ao ar e os destaques e a personalização voltam como estavam."
    else:
        quando = "hoje" if dias == 0 else "amanhã" if dias == 1 else f"em {dias} dias"
        assunto = f"Seu plano Profissional vence {quando}"
        abre = f"O plano Profissional de {nome} vence {quando} ({data})."
        fecha = "Renove para manter tudo como está."
    perde = ("Sem a renovação, a conta volta ao plano Grátis: o limite cai para "
             f"{LIMITES['gratis']['imoveis']} imóveis e {LIMITES['gratis']['fotos']} fotos por imóvel, os imóveis acima disso ficam pausados "
             "(nada é apagado; voltam ao ar quando você renovar) e logo, cor, template da página e imóveis em destaque deixam de valer.")
    texto = f"Olá, {nome}!\n\n{abre}\n\n{perde}\n\n{fecha}\nRenovar: {link}\n\n— {SITE_NOME}\n"
    html = (f"<p>Olá, {html_escape(nome)}!</p><p>{html_escape(abre)}</p><p>{html_escape(perde)}</p><p>{html_escape(fecha)}</p>"
            f'<p><a href="{html_escape(link)}">Renovar meu plano</a></p><p>— {html_escape(SITE_NOME)}</p>')
    return f"[{SITE_NOME}] {assunto}", texto, html


def _tarefa_autorizada():
    enviado = request.args.get("t") or request.headers.get("X-Tarefas-Token", "")
    return bool(TAREFAS_TOKEN and enviado and hmac.compare_digest(enviado.encode(), TAREFAS_TOKEN.encode()))


@app.route("/tarefas/limpar-cache", methods=["GET", "POST"])
def tarefa_limpar_cache():
    """Cron (ex.: 1x/semana): apaga do cache as variantes de imagem sem uso há N dias (?dias=60, mín. 7). É seguro:
    elas são recriadas sob demanda no próximo acesso. Também remove .tmp esquecidos."""
    if not _tarefa_autorizada():
        abort(403)
    dias = max(7, min(365, request.args.get("dias", 60, type=int) or 60))
    corte, corte_tmp = time.time() - dias * 86400, time.time() - 86400
    apagados = liberado = 0
    try:
        with os.scandir(storage.cache) as it:
            for e in it:
                try:
                    if not e.is_file(follow_symlinks=False):
                        continue
                    st = e.stat(follow_symlinks=False)
                    velho = (st.st_mtime < corte_tmp) if e.name.endswith(".tmp") else (max(st.st_atime, st.st_mtime) < corte)
                    if velho:
                        os.remove(e.path)
                        apagados += 1
                        liberado += st.st_size
                except OSError:
                    continue
    except OSError:
        pass
    _CACHE_BYTES["t"] = 0.0
    return jsonify(ok=True, dias=dias, apagados=apagados, liberado=fmt_bytes(liberado))


@app.route("/tarefas/renovacao", methods=["GET", "POST"])
def tarefa_renovacao():
    """Cron diário (externo): avisa por e-mail quem está perto de vencer ou venceu. Idempotente (avisos_assinatura)."""
    if not _tarefa_autorizada():
        abort(403)
    pausados, reativados = sincronizar_planos()        # Pro vencido → excedente pausado (não depende de SMTP)
    _conn().commit()
    if not (SMTP_USER and SMTP_SENHA):
        return jsonify(ok=False, pausados=pausados, reativados=reativados,
                       erro="SMTP não configurado: nenhum e-mail foi enviado nem marcado como enviado."), 503
    link = base_url() + url_for("anunciante_assinatura")
    h = hoje()
    enviados, erros = {"aviso_5d": 0, "aviso_1d": 0, "vencido": 0}, 0
    for r in query_all("SELECT t.id, t.nome, t.plano_vencimento, u.email, EXISTS (SELECT 1 FROM assinaturas a WHERE a.tenant_id = t.id "
                       f"AND a.recorrente AND a.status = 'ativa' AND a.vencimento >= '{h.isoformat()}'::date) AS recorrente "
                       "FROM tenants t JOIN usuarios u ON u.id = t.usuario_id "
                       "WHERE t.plano = 'profissional' AND t.assinatura_status = 'ativa' AND t.plano_vencimento IS NOT NULL "
                       "AND t.status = 'ativo'"):
        dias = (r["plano_vencimento"] - h).days
        tipo = "aviso_5d" if 2 <= dias <= 5 else "aviso_1d" if 0 <= dias <= 1 else "vencido" if -3 <= dias < 0 else None
        if not tipo or not r["email"]:
            continue
        if r["recorrente"] and tipo != "vencido":        # renova sozinho no cartão: só avisa se a renovação falhar
            continue
        try:
            novo = execute_returning("INSERT INTO avisos_assinatura (tenant_id, tipo, vencimento) VALUES (%s,%s,%s) "
                                     "ON CONFLICT DO NOTHING RETURNING id", (r["id"], tipo, r["plano_vencimento"]))
            _conn().commit()                          # grava já: se algo falhar adiante, este aviso não se repete
            if novo:
                assunto, texto, html = _email_renovacao(tipo, r["nome"], r["plano_vencimento"], dias, link)
                _enviar_email(assunto, texto, para=r["email"], html=html)
                enviados[tipo] += 1
        except Exception:
            erros += 1
            log.exception("Falha no aviso de renovação (tenant %s)", r["id"])
            try:
                _conn().rollback()
            except Exception:
                pass
    return jsonify(ok=True, enviados=enviados, total=sum(enviados.values()), erros=erros, pausados=pausados, reativados=reativados)


@app.route("/webhooks/pagbank", methods=["POST"])
def webhook_pagbank():
    bruto = request.get_data()
    if not pagbank.autentico(bruto):
        abort(403)
    try:
        dados = json.loads(bruto or b"{}")
    except ValueError:
        abort(400)
    ev = pagbank.interpretar(dados)
    m = re.match(r"^assin-(\d+)$", ev["referencia"] or "")
    ass = query_one("SELECT * FROM assinaturas WHERE id = %s", (int(m.group(1)),)) if m else None
    novo = execute("INSERT INTO pagamentos (tenant_id, provedor, evento_id, referencia, status, valor, payload) "
                   "VALUES (%s,'pagbank',%s,%s,%s,%s,%s) ON CONFLICT (provedor, evento_id) DO NOTHING",
                   (ass["tenant_id"] if ass else None, ev["evento_id"], ev["referencia"], ev["status"], ev["valor"],
                    json.dumps(dados)))
    if novo and ass:                       # evento repetido não renova de novo (idempotente)
        try:
            if ev["status"] == "pago":
                order_id = dados.get("id") if str(dados.get("id", "")).startswith("ORDE_") else None
                if PAGBANK_CONFIRMAR and not (order_id and pagbank.pagamento_confere(order_id, ass)):
                    log.warning("Pagamento NÃO confirmado na API do PagBank (assinatura %s, pedido %s): não liberei", ass["id"], order_id)
                    execute("DELETE FROM pagamentos WHERE provedor = 'pagbank' AND evento_id = %s", (ev["evento_id"],))
                    return jsonify(ok=True, confirmado=False)
                ativar_profissional(ass["tenant_id"], assinatura_id=ass["id"], provedor="pagbank", meses=ass.get("meses"))
            elif ev["status"] == "falhou" and ass["status"] == "ativa":
                desativar_profissional(ass["tenant_id"], "atrasada")
        except Exception:
            log.exception("Erro ao processar aviso do PagBank; devolvi 500 para ele tentar de novo")
            execute("DELETE FROM pagamentos WHERE provedor = 'pagbank' AND evento_id = %s", (ev["evento_id"],))
            return jsonify(ok=False), 500
    return jsonify(ok=True)


def _assinatura_da_notificacao(ev):
    """Acha a linha de `assinaturas` de um aviso: pelo id da assinatura, pelo id do plano (cada checkout cria o seu) ou pela referência."""
    if ev["assinatura_id"]:
        a = query_one("SELECT * FROM assinaturas WHERE assinatura_externa_id = %s ORDER BY id DESC LIMIT 1", (ev["assinatura_id"],))
        if a:
            return a
    if ev["plano_id"]:
        a = query_one("SELECT * FROM assinaturas WHERE plano_externo_id = %s ORDER BY id DESC LIMIT 1", (ev["plano_id"],))
        if a:
            return a
    m = re.match(r"^assin-(\d+)$", ev["reference_id"] or "")
    return query_one("SELECT * FROM assinaturas WHERE id = %s", (int(m.group(1)),)) if m else None


def _aplicar_evento_assinatura(ass, ev):
    """O Pro vale até a data já paga. Cancelar/suspender/atrasar NÃO derruba na hora: só deixa de estender."""
    evt, st = ev["evento"], ev["status"]
    if evt in ("subscription.canceled", "subscription.expired") or st in ("CANCELED", "EXPIRED"):
        execute("UPDATE assinaturas SET status = %s, atualizado_em = NOW() WHERE id = %s",
                ("cancelada" if "CANCEL" in (evt + st).upper() else "expirada", ass["id"]))
    elif evt == "subscription.suspended" or st == "SUSPENDED":
        execute("UPDATE assinaturas SET status = 'suspensa', atualizado_em = NOW() WHERE id = %s", (ass["id"],))
    elif st == "OVERDUE":                               # cobrança negada (o PagBank tenta de novo conforme as retentativas)
        execute("UPDATE assinaturas SET status = 'atrasada', atualizado_em = NOW() WHERE id = %s", (ass["id"],))
    elif st == "ACTIVE" and ev["proxima"]:
        renovar_ate(ass["tenant_id"], ev["proxima"] + timedelta(days=RECORRENCIA_TOLERANCIA_DIAS), ass["id"], ev["proxima"])


@app.route("/webhooks/pagbank-assinaturas", methods=["POST"])
def webhook_pagbank_assinaturas():
    """Avisos de assinatura do PagBank (subscription.recurrence etc.). Cadastre esta URL, com ?t=<PAGBANK_WEBHOOK_TOKEN>,
    nas preferências de notificação do Painel de Recorrência."""
    bruto = request.get_data()
    if not pagbank.autentico(bruto):
        abort(403)
    try:
        dados = json.loads(bruto or b"{}")
    except ValueError:
        abort(400)
    ev = pagbank.interpretar_assinatura(dados)
    if not ev["evento"].startswith("subscription."):
        return jsonify(ok=True, ignorado=ev["evento"])
    ass = _assinatura_da_notificacao(ev)
    evento_id = f"{ev['evento']}:{ev['assinatura_id']}:{ev['atualizado'] or ev['proxima'] or ''}:{ev['status']}"
    novo = execute("INSERT INTO pagamentos (tenant_id, provedor, evento_id, referencia, status, valor, payload) "
                   "VALUES (%s,'pagbank-assinatura',%s,%s,%s,%s,%s) ON CONFLICT (provedor, evento_id) DO NOTHING",
                   (ass["tenant_id"] if ass else None, evento_id, ev["assinatura_id"], ev["status"].lower() or "?", ev["valor"], json.dumps(dados)))
    if not novo:
        return jsonify(ok=True, repetido=True)
    if not ass:
        log.warning("Aviso de assinatura sem anunciante correspondente: %s (%s)", ev["assinatura_id"], ev["evento"])
        return jsonify(ok=True, sem_anunciante=True)
    if ev["assinatura_id"] and not ass.get("assinatura_externa_id"):
        execute("UPDATE assinaturas SET assinatura_externa_id = %s WHERE id = %s", (ev["assinatura_id"], ass["id"]))
    _aplicar_evento_assinatura(ass, ev)
    return jsonify(ok=True)


# ═══════════════════════════════════════════════════════════════
# 6b. ADMIN
# ═══════════════════════════════════════════════════════════════

@app.route("/admin")
@admin_required
def admin_dashboard():
    n = query_one(
        """SELECT (SELECT COUNT(*) FROM usuarios) AS usuarios,
                  (SELECT COUNT(*) FROM usuarios WHERE criado_em >= NOW() - INTERVAL '30 days') AS usuarios_30,
                  (SELECT COUNT(*) FROM imoveis WHERE status = 'publicado') AS imoveis,
                  (SELECT COUNT(*) FROM imoveis WHERE status = 'publicado' AND criado_em >= NOW() - INTERVAL '30 days') AS imoveis_30,
                  (SELECT COUNT(*) FROM tenants) AS tenants,
                  (SELECT COUNT(*) FROM tenants WHERE criado_em >= NOW() - INTERVAL '30 days') AS tenants_30,
                  (SELECT COUNT(*) FROM imoveis WHERE status = 'pendente') AS pend_imoveis,
                  (SELECT COUNT(*) FROM imovel_proximos WHERE status = 'pendente') AS pend_proximos,
                  (SELECT COUNT(*) FROM tenants WHERE plano = 'profissional' AND assinatura_status = 'ativa') AS pro,
                  (SELECT COUNT(*) FROM tenants WHERE plano = 'gratis') AS gratis""")
    receita = n["pro"] * PRECO_PROFISSIONAL
    total_planos = max(1, n["pro"] + n["gratis"])
    recentes = query_all("SELECT t.*, (SELECT COUNT(*) FROM imoveis i WHERE i.tenant_id = t.id) AS n_imoveis "
                         "FROM tenants t ORDER BY t.criado_em DESC LIMIT 5")
    atividade = query_all(
        """(SELECT 'imovel' AS k, t.nome AS quem, i.titulo AS o_que, i.criado_em AS quando FROM imoveis i JOIN tenants t ON t.id = i.tenant_id)
           UNION ALL (SELECT 'conta', nome, tipo, criado_em FROM tenants)
           UNION ALL (SELECT 'pagamento', COALESCE(t.nome, '—'), p.status, p.criado_em FROM pagamentos p LEFT JOIN tenants t ON t.id = p.tenant_id)
           ORDER BY quando DESC LIMIT 6""")
    tabela = query_all(
        "SELECT t.*, u.email, (SELECT COUNT(*) FROM imoveis i WHERE i.tenant_id = t.id) AS n_imoveis "
        "FROM tenants t JOIN usuarios u ON u.id = t.usuario_id ORDER BY t.criado_em DESC LIMIT 5")
    return render_template("admin.html", n=n, receita=receita, pct_pro=round(n["pro"] / total_planos * 100),
                           recentes=recentes, atividade=atividade, tabela=tabela, pagina_ativa="dashboard")


@app.route("/admin/anunciantes")
@admin_required
def admin_anunciantes():
    q = sanitize_input(request.args.get("q", "").strip())[:80]
    plano = request.args.get("plano", "")
    status = request.args.get("status", "")
    where, params = ["TRUE"], []
    if q:
        where.append("(t.nome ILIKE %s OR u.email ILIKE %s)"); params += [f"%{q}%", f"%{q}%"]
    if plano in PLANOS:
        where.append("t.plano = %s"); params.append(plano)
    if status in ("ativo", "bloqueado"):
        where.append("t.status = %s"); params.append(status)
    lista = query_all(
        f"SELECT t.*, u.email, (SELECT COUNT(*) FROM imoveis i WHERE i.tenant_id = t.id) AS n_imoveis "
        f"FROM tenants t JOIN usuarios u ON u.id = t.usuario_id WHERE {' AND '.join(where)} ORDER BY t.criado_em DESC LIMIT 200",
        params)
    for t in lista:
        t["plano_efetivo"] = plano_efetivo(t)
    return render_template("admin.html", lista=lista, q=q, plano=plano, status=status, planos_admin=planos_pro(False), pagina_ativa="anunciantes")


@app.route("/admin/anunciantes/<int:tenant_id>/<acao>", methods=["POST"])
@admin_required
def admin_anunciante_acao(tenant_id, acao):
    if not query_one("SELECT 1 FROM tenants WHERE id = %s", (tenant_id,)):
        abort(404)
    if acao == "bloquear":
        execute("UPDATE tenants SET status = 'bloqueado' WHERE id = %s", (tenant_id,))
    elif acao == "desbloquear":
        execute("UPDATE tenants SET status = 'ativo' WHERE id = %s", (tenant_id,))
    elif acao == "verificar":
        execute("UPDATE tenants SET verificada = NOT verificada WHERE id = %s", (tenant_id,))
    elif acao == "ativar_pro":
        p = plano_pro(request.form.get("plano", ""), so_ativo=False)       # admin pode usar até um período desligado
        if p:
            ativar_profissional(tenant_id, provedor="manual", meses=p["meses"], valor=p["valor"], plano_codigo=p["codigo"])
        else:
            ativar_profissional(tenant_id, provedor="manual")
    elif acao == "definir_vencimento":
        try:
            nova = date.fromisoformat(request.form.get("vencimento", ""))
        except ValueError:
            nova = None
        pro = query_one("SELECT plano FROM tenants WHERE id = %s", (tenant_id,))["plano"] == "profissional"
        if not pro or not nova or not (hoje() <= nova <= hoje() + timedelta(days=3660)):
            flash("Data inválida (use uma data de hoje em diante) ou o anunciante não está no Profissional.", "error")
            return _admin_voltar("admin_anunciantes")
        execute("UPDATE tenants SET plano_vencimento = %s WHERE id = %s", (nova, tenant_id))
        execute("UPDATE assinaturas SET vencimento = %s WHERE id = (SELECT id FROM assinaturas WHERE tenant_id = %s AND status = 'ativa' "
                "ORDER BY id DESC LIMIT 1)", (nova, tenant_id))
        sincronizar_planos(tenant_id)
    elif acao == "pro_sem_vencimento":
        execute("UPDATE tenants SET plano = 'profissional', assinatura_status = 'ativa', plano_vencimento = NULL WHERE id = %s", (tenant_id,))
        execute("INSERT INTO assinaturas (tenant_id, plano, status, valor, vencimento, provedor_pagamento, plano_codigo) "
                "VALUES (%s,'profissional','ativa',0,NULL,'manual','sem_vencimento')", (tenant_id,))
        sincronizar_planos(tenant_id)
    elif acao == "voltar_gratis":
        execute("UPDATE tenants SET plano = 'gratis', plano_vencimento = NULL WHERE id = %s", (tenant_id,))
        desativar_profissional(tenant_id)
    else:
        abort(404)
    flash("Feito.", "success")
    return _admin_voltar("admin_anunciantes")


def _admin_voltar(padrao):
    ref = urlparse(request.referrer or "")
    if ref.netloc == request.host and seguro_next(ref.path):
        return redirect(ref.path + ("?" + ref.query if ref.query else ""))
    return redirect(url_for(padrao))


@app.route("/admin/imoveis")
@admin_required
def admin_imoveis():
    q = sanitize_input(request.args.get("q", "").strip())[:80]
    status = request.args.get("status", "")
    where, params = ["TRUE"], []
    if q:
        where.append("(i.titulo ILIKE %s OR t.nome ILIKE %s OR i.bairro ILIKE %s)"); params += [f"%{q}%"] * 3
    if status in STATUS_IMOVEL:
        where.append("i.status = %s"); params.append(status)
    lista = query_all(
        f"SELECT i.*, t.nome AS anunciante_nome, {CAPA_SQL} AS capa FROM imoveis i JOIN tenants t ON t.id = i.tenant_id "
        f"WHERE {' AND '.join(where)} ORDER BY (i.status = 'pendente') DESC, i.criado_em DESC LIMIT 200", params)
    for i in lista:
        i["foto"] = url_foto(i["capa"], 200)
    return render_template("admin.html", lista=lista, q=q, status=status, pagina_ativa="imoveis")


@app.route("/admin/imoveis/<int:imovel_id>/<acao>", methods=["POST"])
@admin_required
def admin_imovel_acao(imovel_id, acao):
    im = query_one("SELECT * FROM imoveis WHERE id = %s", (imovel_id,))
    if not im:
        abort(404)
    if acao == "aprovar":
        execute("UPDATE imoveis SET status = 'publicado', motivo_rejeicao = NULL, atualizado_em = NOW() WHERE id = %s", (imovel_id,))
    elif acao == "rejeitar":
        motivo = sanitize_input(request.form.get("motivo", "").strip())[:300] or "Não atende às regras do portal."
        execute("UPDATE imoveis SET status = 'rejeitado', motivo_rejeicao = %s, atualizado_em = NOW() WHERE id = %s", (motivo, imovel_id))
    elif acao == "pausar":
        execute("UPDATE imoveis SET status = 'pausado', pausado_plano = FALSE, atualizado_em = NOW() WHERE id = %s", (imovel_id,))
    elif acao == "destaque":
        execute("UPDATE imoveis SET destaque = NOT destaque WHERE id = %s", (imovel_id,))
    elif acao == "mcmv":        # selo "Aceita MCMV": só a equipe marca (e só vale para venda)
        if not im["aceita_mcmv"] and im["finalidade"] != "venda":
            flash("O selo Minha Casa Minha Vida só vale para imóveis à venda.", "error")
            return _admin_voltar("admin_imoveis")
        execute("UPDATE imoveis SET aceita_mcmv = NOT aceita_mcmv WHERE id = %s", (imovel_id,))
    elif acao == "excluir":
        arquivos = [f["file_id"] for f in query_all("SELECT file_id FROM imovel_fotos WHERE imovel_id = %s", (imovel_id,))]
        execute("DELETE FROM imoveis WHERE id = %s", (imovel_id,))
        for fid in arquivos:
            storage.deletar_imagem(fid)
    else:
        abort(404)
    flash("Feito.", "success")
    return _admin_voltar("admin_imoveis")


@app.route("/admin/aprovacoes")
@admin_required
def admin_aprovacoes():
    imoveis = query_all(
        f"SELECT i.*, t.nome AS anunciante_nome, {CAPA_SQL} AS capa FROM imoveis i JOIN tenants t ON t.id = i.tenant_id "
        "WHERE i.status = 'pendente' ORDER BY i.criado_em")
    for i in imoveis:
        i["foto"] = url_foto(i["capa"], 200)
    proximos = query_all(
        "SELECT p.*, i.titulo, i.slug, t.nome AS anunciante_nome FROM imovel_proximos p JOIN imoveis i ON i.id = p.imovel_id "
        "JOIN tenants t ON t.id = i.tenant_id WHERE p.status = 'pendente' ORDER BY p.id")
    return render_template("admin.html", imoveis=imoveis, proximos=proximos, pagina_ativa="aprovacoes")


@app.route("/admin/proximos/<int:prox_id>/<acao>", methods=["POST"])
@admin_required
def admin_proximo_acao(prox_id, acao):
    if acao == "aprovar":
        execute("UPDATE imovel_proximos SET status = 'aprovado' WHERE id = %s", (prox_id,))
    elif acao == "rejeitar":
        execute("DELETE FROM imovel_proximos WHERE id = %s", (prox_id,))
    else:
        abort(404)
    flash("Feito.", "success")
    return _admin_voltar("admin_aprovacoes")


@app.route("/admin/planos", methods=["GET", "POST"])
@admin_required
def admin_planos():
    """Preço e liga/desliga de cada período do Profissional. Muda o que se cobra de quem assinar dali pra frente, então pede a senha
    do admin de novo. Assinaturas de cartão já criadas no PagBank NÃO mudam de valor. O mensal é obrigatório (não desliga)."""
    if request.method == "POST":
        if not check_password_hash(g.usuario["senha_hash"], request.form.get("senha_atual", "")):
            flash("Senha incorreta — nada foi salvo.", "error")
            return redirect(url_for("admin_planos"))
        novos = {}
        for p in planos_pro(False):
            valor = num_br(request.form.get(f"valor_{p['codigo']}"))
            if valor is None or not (1 <= valor <= 100000):
                flash(f"Preço inválido em {p['nome']}: nada foi salvo.", "error")
                return redirect(url_for("admin_planos"))
            novos[p["codigo"]] = (round(valor, 2), p["codigo"] == "mensal" or request.form.get(f"ativo_{p['codigo']}") == "1")
        for cod, (valor, ativo) in novos.items():
            execute("UPDATE planos_pro SET valor = %s, ativo = %s WHERE codigo = %s", (valor, ativo, cod))
        g.pop("planos_pro", None)
        flash("Planos atualizados. O novo preço vale para quem assinar a partir de agora; assinaturas de cartão já ativas mantêm o valor antigo.", "success")
        return redirect(url_for("admin_planos"))
    return render_template("admin.html", planos=planos_pro(False), pagina_ativa="planos")


@app.route("/admin/assinaturas")
@admin_required
def admin_assinaturas():
    assinaturas = query_all(
        "SELECT a.*, t.nome AS tenant_nome FROM assinaturas a JOIN tenants t ON t.id = a.tenant_id ORDER BY a.criado_em DESC LIMIT 100")
    pagamentos = query_all(
        "SELECT p.*, t.nome AS tenant_nome FROM pagamentos p LEFT JOIN tenants t ON t.id = p.tenant_id ORDER BY p.criado_em DESC LIMIT 100")
    return render_template("admin.html", assinaturas=assinaturas, pagamentos=pagamentos,
                           pagbank_ok=pagbank.configurado(), pagina_ativa="assinaturas")


# ═══════════════════════════════════════════════════════════════
# LEADS (admin): lista, filtros e exportação para Excel
# ═══════════════════════════════════════════════════════════════

CANAIS_LEAD = {"whatsapp": "WhatsApp", "telefone": "Telefone", "visita": "Visita"}
ORIGENS_LEAD = {"imovel": "Página do imóvel", "imobiliaria": "Página da imobiliária"}
COOKIES_LEAD = {"todos": "Aceitou todos", "essenciais": "Só essenciais"}
LEADS_POR_PAGINA = 50


def _leads_filtro(args):
    q = sanitize_input(args.get("q", "").strip())[:80]
    canal, origem = args.get("canal", ""), args.get("origem", "")
    dias = args.get("dias", "30")
    tenant_id = args.get("anunciante", type=int)
    where, params = ["TRUE"], []
    if q:
        where.append("(l.nome ILIKE %s OR l.email ILIKE %s OR l.telefone ILIKE %s OR l.imovel_titulo ILIKE %s)")
        params += [f"%{q}%"] * 3 + [f"%{q}%"]
    if canal in CANAIS_LEAD:
        where.append("l.canal = %s"); params.append(canal)
    if origem in ORIGENS_LEAD:
        where.append("l.origem = %s"); params.append(origem)
    if tenant_id:
        where.append("l.tenant_id = %s"); params.append(tenant_id)
    if dias in ("7", "30", "90"):
        where.append("l.criado_em >= NOW() - %s * INTERVAL '1 day'"); params.append(int(dias))
    else:
        dias = "todos"
    verificado = "1" if args.get("verificado") == "1" else ""
    if verificado:
        where.append("l.email_verificado")
    return " AND ".join(where), params, dict(q=q, canal=canal, origem=origem, dias=dias, anunciante=tenant_id or "", verificado=verificado)


_LEADS_SQL = ("SELECT l.*, u.criado_em AS cadastro_em FROM leads l LEFT JOIN usuarios u ON u.id = l.usuario_id "
              "WHERE {where} ORDER BY l.criado_em DESC")


@app.route("/admin/leads")
@admin_required
def admin_leads():
    where, params, f = _leads_filtro(request.args)
    pagina = max(1, request.args.get("pagina", 1, type=int))
    resumo = query_one(
        f"SELECT COUNT(*) AS total, COUNT(DISTINCT l.usuario_id) AS pessoas, COUNT(DISTINCT l.tenant_id) AS anunciantes, COUNT(*) FILTER (WHERE l.email_verificado) AS verificados, "
        f"COUNT(*) FILTER (WHERE l.criado_em >= NOW() - INTERVAL '7 days') AS ult7 FROM leads l WHERE {where}", params)
    lista = query_all(_LEADS_SQL.format(where=where) + " LIMIT %s OFFSET %s",
                      params + [LEADS_POR_PAGINA, (pagina - 1) * LEADS_POR_PAGINA])
    paginas = max(1, math.ceil(resumo["total"] / LEADS_POR_PAGINA))
    anunciantes = query_all("SELECT DISTINCT t.id, t.nome FROM leads l JOIN tenants t ON t.id = l.tenant_id ORDER BY t.nome")
    qs = {k: v for k, v in request.args.items() if k != "pagina" and v}
    return render_template("admin.html", pagina_ativa="leads", leads=lista, resumo=resumo, f=f, pagina=pagina,
                           paginas=paginas, anunciantes_lista=anunciantes, qs_filtro=qs, wa_link=wa_link,
                           CANAIS_LEAD=CANAIS_LEAD, ORIGENS_LEAD=ORIGENS_LEAD, COOKIES_LEAD=COOKIES_LEAD)


def _local_naive(dt):
    return dt.astimezone(TZ).replace(tzinfo=None) if dt else None


@app.route("/admin/leads/exportar")
@admin_required
def admin_leads_exportar():
    where, params, _ = _leads_filtro(request.args)
    sql = _LEADS_SQL.format(where=where) + " LIMIT 50000"

    def linhas():
        """Lê do banco em lotes (cursor no servidor): as 50 mil linhas nunca ficam todas na memória ao mesmo tempo."""
        with _conn().cursor(name="leads_exportar", cursor_factory=psycopg2.extras.RealDictCursor) as c:
            c.itersize = 1000
            c.execute(sql, params)
            for r in c:
                yield r

    cab = ["Data do contato", "Nome", "E-mail", "Telefone", "Cidade do lead", "Cadastro do lead em", "Origem", "Canal",
           "Imóvel", "Finalidade", "Tipo", "Preço (R$)", "Quartos", "Cidade do imóvel", "Bairro do imóvel", "Anunciante",
           "Detalhe (visita)", "Cookies", "E-mail verificado", "Telefone verificado", "Consentimento (LGPD) em"]

    def linha(r):
        return [_local_naive(r["criado_em"]), r["nome"], r["email"], r["telefone"], r["cidade"], _local_naive(r["cadastro_em"]),
                ORIGENS_LEAD.get(r["origem"], r["origem"]), CANAIS_LEAD.get(r["canal"], r["canal"]),
                r["imovel_titulo"], FINALIDADES.get(r["finalidade"], r["finalidade"]), TIPOS_IMOVEL.get(r["tipo_imovel"], r["tipo_imovel"]),
                float(r["preco"]) if r["preco"] is not None else None, r["quartos"], r["cidade_imovel"], r["bairro_imovel"],
                r["tenant_nome"], r["detalhe"], COOKIES_LEAD.get(r["cookies"], "—"),
                "Sim" if r["email_verificado"] else "Não", "Sim" if r["telefone_verificado"] else "Não", _local_naive(r["consentimento_em"])]

    nome = f"leads-ondeimoveis-{datetime.now(TZ).strftime('%Y-%m-%d')}"
    try:
        from openpyxl import Workbook
        from openpyxl.cell import WriteOnlyCell
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:                       # sem openpyxl no servidor: CSV que o Excel abre direto (; e acentos ok)
        import csv
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(cab)
        for r in linhas():
            w.writerow([v.strftime("%d/%m/%Y %H:%M") if isinstance(v, datetime) else ("" if v is None else v) for v in linha(r)])
        return Response("\ufeff" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{nome}.csv"'})
    # write_only: grava linha a linha em disco temporário (a planilha inteira não vive na RAM)
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Leads")
    larguras = [17, 26, 32, 16, 18, 17, 22, 12, 36, 11, 13, 14, 8, 18, 18, 26, 30, 14, 16, 17, 22]
    for k, w_ in enumerate(larguras, 1):
        ws.column_dimensions[get_column_letter(k)].width = w_
    ws.freeze_panes = "A2"
    topo = []
    for titulo in cab:
        c = WriteOnlyCell(ws, value=titulo)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="071521")
        c.alignment = Alignment(vertical="center")
        topo.append(c)
    ws.append(topo)
    formatos = {0: "dd/mm/yyyy hh:mm", 5: "dd/mm/yyyy hh:mm", 20: "dd/mm/yyyy hh:mm", 11: "#,##0.00", 3: "@"}
    n = 1
    for r in linhas():
        saida = []
        for k, v in enumerate(linha(r)):
            if k in formatos:
                c = WriteOnlyCell(ws, value=v)
                c.number_format = formatos[k]
                saida.append(c)
            else:
                saida.append(v)
        ws.append(saida)
        n += 1
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cab))}{n}"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"{nome}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/admin/arquivos")
@admin_required
def admin_arquivos():
    lista = query_all(
        """SELECT a.*, (EXISTS (SELECT 1 FROM imovel_fotos f WHERE f.file_id = a.id)
                        OR EXISTS (SELECT 1 FROM tenants t WHERE t.logo_file_id = a.id)) AS em_uso
             FROM arquivos a ORDER BY a.criado_em DESC LIMIT 120""")
    resumo = query_one("SELECT COUNT(*) AS n, COALESCE(SUM(tamanho), 0) AS bytes FROM arquivos")
    orfaos = query_one("""SELECT COUNT(*) AS n FROM arquivos a WHERE NOT EXISTS (SELECT 1 FROM imovel_fotos f WHERE f.file_id = a.id)
                          AND NOT EXISTS (SELECT 1 FROM tenants t WHERE t.logo_file_id = a.id)""")["n"]
    return render_template("admin.html", lista=lista, resumo=resumo, orfaos=orfaos, vol=volume_info(), pagina_ativa="arquivos",
                           uso_anunciantes=uso_anunciantes(), disco=disco_info(), cache_bytes=cache_bytes_cached(),
                           originais_bytes=int(resumo["bytes"]))


@app.route("/admin/arquivos/limpar", methods=["POST"])
@admin_required
def admin_arquivos_limpar():
    ids = [r["id"] for r in query_all(
        """SELECT a.id FROM arquivos a WHERE a.criado_em < NOW() - INTERVAL '1 hour'
             AND NOT EXISTS (SELECT 1 FROM imovel_fotos f WHERE f.file_id = a.id)
             AND NOT EXISTS (SELECT 1 FROM tenants t WHERE t.logo_file_id = a.id)""")]
    for fid in ids:
        storage.deletar_imagem(fid)
    flash(f"{len(ids)} arquivo(s) órfão(s) removido(s).", "success")
    return redirect(url_for("admin_arquivos"))


@app.route("/admin/midia")
@admin_required
def admin_midia():
    idx = _site_indice()
    base = base_url()
    slots = []
    for k, cfg in SITE_SLOTS.items():
        f = idx.get(k)
        slots.append(dict(cfg, chave=k, url=url_for("site_media", nome=f[0], v=f[1]) if f else None))
    avulsas = sorted(((b, f, v) for b, (f, v) in idx.items() if b.startswith("img-")), key=lambda x: -x[2])
    avulsas = [dict(nome=f, url=url_for("site_media", nome=f, v=v), completa=base + url_for("site_media", nome=f)) for _, f, v in avulsas]
    return render_template("admin.html", slots=slots, avulsas=avulsas, vol=volume_info(), pagina_ativa="midia")


@app.route("/admin/midia/enviar", methods=["POST"])
@admin_required
def admin_midia_enviar():
    slot = request.form.get("slot") or None
    if slot and slot not in SITE_SLOTS:
        abort(400)
    arquivos = [a for a in request.files.getlist("arquivo") if a and a.filename]
    if not arquivos:
        flash("Escolha um arquivo.", "error")
        return redirect(url_for("admin_midia"))
    for a in (arquivos[:1] if slot else arquivos[:20]):
        try:
            site_salvar_imagem(a, slot)
            flash(f"{SITE_SLOTS[slot]['nome']} atualizado(a)." if slot else f"“{a.filename}” enviada.", "success")
        except Exception as e:
            log.exception("upload do site falhou (%s)", a.filename)
            if isinstance(e, ValueError):
                motivo = str(e)
            elif isinstance(e, PermissionError):
                motivo = f"sem permissão para gravar em {SITE_DIR} (ajuste o dono da pasta/volume)"
            elif isinstance(e, OSError) and e.__class__.__name__ == "UnidentifiedImageError":
                motivo = "não é uma imagem válida — use PNG, JPG ou WEBP (AVIF/HEIC não funcionam)"
            elif isinstance(e, OSError):
                motivo = f"erro ao gravar o arquivo ({e.strerror or e})"
            else:
                motivo = f"erro inesperado ({e.__class__.__name__}) — veja os logs do container"
            flash(f"“{a.filename}”: {motivo}.", "error")
    return redirect(url_for("admin_midia"))


@app.route("/admin/midia/remover", methods=["POST"])
@admin_required
def admin_midia_remover():
    site_remover_imagem(request.form.get("nome", ""))
    flash("Arquivo removido.", "success")
    return redirect(url_for("admin_midia"))


# ═══════════════════════════════════════════════════════════════
# 6c. CHATBOT — indica o imóvel ideal (formulário rápido ou conversa com IA / Groq)
#     Chaves no .env: GROQ_API_KEY e GROQ_API_KEY_2 (reserva). Modelo: GROQ_MODEL (opcional).
#     Tudo é salvo: sessões, mensagens, respostas do formulário e imóveis indicados. Nada é apagado.
# ═══════════════════════════════════════════════════════════════

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELO = os.environ.get("GROQ_MODEL", "").strip() or "openai/gpt-oss-120b"

PERSONA_PADRAO = (
    "Você é {nome}, consultora de imóveis do {site}. Fala português do Brasil, de um jeito simpático, leve e humano, "
    "como uma amiga que entende de imóvel. Respostas CURTAS: no máximo 2 frases, no máximo 1 pergunta por vez. "
    "Cumprimente de volta quando a pessoa cumprimentar (bom dia, tudo bem?) e logo conduza a conversa para descobrir: "
    "comprar ou alugar, tipo de imóvel, bairro/cidade, orçamento, quartos, vagas e o que não pode faltar. "
    "Não use listas nem textos longos. Use no máximo 1 emoji por mensagem.")

CHAT_PADRAO = {
    "ativo": "1", "nome": "Duda", "temperatura": "0.6", "persona": PERSONA_PADRAO,
    "boas_vindas": "Oi! Eu sou a {nome} 👋 Vou te ajudar a achar o imóvel perfeito. Como você prefere?",
    "ia_abertura": "{saudacao}! Tudo bem? 😊 Me conta o que você está procurando.",
    "qtd_resultados": "3", "max_msgs": "40"}

_chat_cfg_cache = {"t": 0, "v": None}


def chat_cfg(forcar=False):
    """Configuração do chatbot (banco + padrões). Cache curto para não consultar a cada página."""
    if not forcar and _chat_cfg_cache["v"] and time.time() - _chat_cfg_cache["t"] < 20:
        return _chat_cfg_cache["v"]
    bruto = dict(CHAT_PADRAO)
    try:
        for r in query_all("SELECT chave, valor FROM chatbot_config"):
            if r["chave"] in bruto:
                bruto[r["chave"]] = r["valor"]
    except Exception:
        log.exception("chat_cfg")
    try:
        temp = min(1.5, max(0.0, float(str(bruto["temperatura"]).replace(",", "."))))
    except ValueError:
        temp = 0.6
    try:
        qtd = min(5, max(1, int(bruto["qtd_resultados"])))
    except ValueError:
        qtd = 3
    try:
        mx = min(200, max(6, int(bruto["max_msgs"])))
    except ValueError:
        mx = 40
    cfg = dict(ativo=bruto["ativo"], nome=(bruto["nome"] or "Duda")[:30], temp=temp, qtd=qtd, max_msgs_n=mx,
               persona=bruto["persona"] or PERSONA_PADRAO, boas_vindas=bruto["boas_vindas"], ia_abertura=bruto["ia_abertura"])
    _chat_cfg_cache.update(t=time.time(), v=cfg)
    return cfg


def chat_cfg_set(chave, valor, usuario_id=None):
    """Grava a configuração (upsert). Persona nova → guarda no histórico. Nunca apaga nada."""
    atual = query_one("SELECT valor FROM chatbot_config WHERE chave = %s", (chave,))
    execute("INSERT INTO chatbot_config (chave, valor, atualizado_em) VALUES (%s,%s,NOW()) "
            "ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor, atualizado_em = NOW()", (chave, valor))
    if chave == "persona" and (atual is None or atual["valor"] != valor):
        execute("INSERT INTO chatbot_config_historico (chave, valor, usuario_id) VALUES (%s,%s,%s)", (chave, valor, usuario_id))


@app.context_processor
def _chat_ctx():
    def ativo():
        try:
            return chat_cfg()["ativo"] == "1"
        except Exception:
            return False
    return dict(chat_ativo=ativo, chat_pub=lambda: dict(nome=chat_cfg()["nome"], ia=bool(_groq_chaves())))


# ---------- Groq (2 chaves, com reserva automática) ----------

def _groq_chaves():
    return [k for k in (os.environ.get("GROQ_API_KEY", "").strip(), os.environ.get("GROQ_API_KEY_2", "").strip()) if k]


_groq_pausa = {}


def _groq_post(chave, payload):
    return requests.post(GROQ_URL, json=payload, timeout=30,
                         headers={"Authorization": f"Bearer {chave}", "Content-Type": "application/json"})


def _groq_payload(mensagens, temp, json_mode, max_tokens=1200):
    p = {"model": GROQ_MODELO, "messages": mensagens, "temperature": temp, "max_tokens": max_tokens}
    if GROQ_MODELO.startswith("openai/gpt-oss"):
        p["reasoning_effort"] = "low"
    if json_mode:
        p["response_format"] = {"type": "json_object"}
    return p


def groq_chat(mensagens, temp=0.6, json_mode=False):
    """Tenta a chave principal; se der erro/limite, usa a reserva. Devolve (texto, tokens, nº da chave)."""
    chaves = _groq_chaves()
    if not chaves:
        raise RuntimeError("nenhuma chave Groq configurada")
    payload = _groq_payload(mensagens, temp, json_mode)
    ordem = [k for k in chaves if _groq_pausa.get(k, 0) <= time.time()] or chaves
    ultimo = None
    for k in ordem:
        try:
            r = _groq_post(k, payload)
            if r.status_code == 400 and json_mode:       # modelo sem JSON mode: tenta sem
                r = _groq_post(k, {x: v for x, v in payload.items() if x != "response_format"})
            if r.status_code == 200:
                d = r.json()
                txt = (d["choices"][0]["message"].get("content") or "").strip()
                if not txt:
                    raise ValueError("resposta vazia")
                return txt, int((d.get("usage") or {}).get("total_tokens") or 0), str(chaves.index(k) + 1)
            espera = 60
            if r.status_code == 429:
                try:
                    espera = min(900, max(5, int(float(r.headers.get("retry-after", 30)))))
                except ValueError:
                    espera = 30
            _groq_pausa[k] = time.time() + espera
            try:
                detalhe = (r.json().get("error") or {}).get("message", "")
            except Exception:
                detalhe = ""
            ultimo = f"HTTP {r.status_code}: {detalhe}"[:200]
            log.warning("Groq: chave %s falhou (%s)", chaves.index(k) + 1, ultimo)
        except Exception as e:
            _groq_pausa[k] = time.time() + 30
            ultimo = f"{type(e).__name__}: {e}"[:200]
            log.warning("Groq: chave %s falhou (%s)", chaves.index(k) + 1, ultimo)
    raise RuntimeError(f"todas as chaves falharam — último erro: {ultimo}")


def _json_da_ia(txt):
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    a, b = txt.find("{"), txt.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("sem JSON")
    d = json.loads(txt[a:b + 1])
    return d if isinstance(d, dict) else {}


# ---------- perfil do cliente, busca e pontuação ----------

EXTRAS_CHAT = ["Piscina", "Sacada", "Churrasqueira", "Elevador", "Aceita pets", "Mobiliado"]
ORCAMENTOS = {
    "venda": [("300000", "Até R$ 300 mil"), ("500000", "Até R$ 500 mil"), ("800000", "Até R$ 800 mil"),
              ("1200000", "Até R$ 1,2 milhão"), ("2000000", "Até R$ 2 milhões"), ("0", "Sem limite")],
    "aluguel": [("1500", "Até R$ 1.500"), ("2500", "Até R$ 2.500"), ("4000", "Até R$ 4.000"),
                ("6000", "Até R$ 6.000"), ("10000", "Até R$ 10.000"), ("0", "Sem limite")]}
PASSOS = ["finalidade", "tipo", "local", "orcamento", "quartos", "vagas", "extras"]


def _valor_ia(v):
    """Número vindo da IA: aceita 500000, "500.000", "500 mil", "1,2 milhão", "800k"."""
    if v in (None, "", "null") or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).lower()
    fator = 1_000_000 if re.search(r"milh|\bmi\b|\bmm\b", t) else 1000 if re.search(r"\bmil\b|\d\s*k\b", t) else 1
    n = num_br(re.sub(r"[^\d,.]", "", t.replace("r$", "")))
    return n * fator if n is not None else None


def _int_ou_none(v, mx=10):
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return min(max(n, 0), mx) if n > 0 else None


def normalizar_perfil(p):
    """Limpa o que a IA devolveu: só valores válidos, tipos seguros."""
    p = p if isinstance(p, dict) else {}
    out = {}
    fin = str(p.get("finalidade") or "").strip().lower()
    if fin in ("venda", "comprar", "compra"):
        out["finalidade"] = "venda"
    elif fin in ("aluguel", "alugar", "locação", "locacao"):
        out["finalidade"] = "aluguel"
    tipo = str(p.get("tipo") or "").strip().lower()
    if tipo in TIPOS_IMOVEL:
        out["tipo"] = tipo
    for k in ("bairro", "cidade"):
        v = sanitize_input(p.get(k) if isinstance(p.get(k), (str, int, float)) else "")[:80]
        if v and v.lower() not in ("null", "none"):
            out[k] = v
    pm = _valor_ia(p.get("preco_max"))
    if pm and pm > 0:
        out["preco_max"] = pm
    for k in ("quartos", "vagas"):
        n = _int_ou_none(p.get(k))
        if n:
            out[k] = n
    ex = p.get("extras")
    ex = ex if isinstance(ex, list) else []
    mapa = {gerar_slug(e): e for e in EXTRAS_CHAT}
    lista = []
    for e in ex:
        e = mapa.get(gerar_slug(str(e)))
        if e and e not in lista:
            lista.append(e)
    if lista:
        out["extras"] = lista
    return out


def resumo_perfil(d):
    if not d:
        return ""
    partes = []
    if d.get("finalidade"):
        partes.append("Comprar" if d["finalidade"] == "venda" else "Alugar")
    if d.get("tipo"):
        partes.append(TIPOS_IMOVEL[d["tipo"]])
    loc = ", ".join(x for x in (d.get("bairro"), d.get("cidade")) if x)
    if loc:
        partes.append(loc)
    if d.get("preco_max"):
        partes.append("até R$ " + _fmt_num(d["preco_max"]))
    if d.get("quartos"):
        partes.append(f"{d['quartos']}+ quartos")
    if d.get("vagas"):
        partes.append(f"{d['vagas']}+ vagas")
    partes += d.get("extras") or []
    return " · ".join(partes)


def link_busca(d):
    from urllib.parse import urlencode
    q = [("finalidade", d.get("finalidade")), ("tipo", d.get("tipo")),
         ("preco_max", int(d["preco_max"]) if d.get("preco_max") else None),
         ("quartos", d.get("quartos")), ("vagas", d.get("vagas")), ("cidade", d.get("cidade")), ("bairro", d.get("bairro"))]
    return base_url() + url_for("buscar") + "?" + urlencode([(k, v) for k, v in q if v not in (None, "")])


def chat_buscar(d, qtd=3):
    """Pontua os imóveis publicados contra o perfil. Devolve (cards, nº de combinações quase perfeitas)."""
    where, params = ["i.status = 'publicado'", "t.status = 'ativo'"], []
    if d.get("finalidade"):
        where.append("i.finalidade = %s"); params.append(d["finalidade"])
    linhas = query_all(
        f"SELECT i.*, {sql_destaque()} AS destaque_efetivo, {CAPA_SQL} AS capa FROM imoveis i JOIN tenants t ON t.id = i.tenant_id "
        f"WHERE {' AND '.join(where)} ORDER BY {sql_destaque()} DESC, i.criado_em DESC LIMIT 400", params)
    bairro_s, cidade_s = gerar_slug(d.get("bairro", "")) if d.get("bairro") else "", gerar_slug(d.get("cidade", "")) if d.get("cidade") else ""
    pontuados = []
    for r in linhas:
        pts, maxi, ok, falta = 0.0, 0, [], []
        if d.get("tipo"):
            maxi += 25
            if r["tipo"] == d["tipo"]:
                pts += 25; ok.append(TIPOS_IMOVEL[r["tipo"]])
        if bairro_s or cidade_s:
            maxi += 25
            if bairro_s and (bairro_s in r["bairro_slug"] or r["bairro_slug"] in bairro_s):
                pts += 25; ok.append(r["bairro"])
            elif bairro_s and cidade_s and r["cidade_slug"] == cidade_s:
                pts += 10; falta.append(f"Fica em {r['bairro']}")
            elif not bairro_s and r["cidade_slug"] == cidade_s:
                pts += 25; ok.append(r["cidade"])
            elif bairro_s and not cidade_s:
                falta.append(f"Fica em {r['bairro']}")
        if d.get("preco_max"):
            maxi += 30
            pr, mx = float(r["preco"] or 0), float(d["preco_max"])
            if pr <= mx:
                pts += 30; ok.append("Dentro do orçamento")
            elif pr <= mx * 1.15:
                pts += 15; falta.append("Um pouco acima do orçamento")
        if d.get("quartos"):
            maxi += 15
            if r["dormitorios"] >= d["quartos"]:
                pts += 15; ok.append(f"{r['dormitorios']} quarto{'s' if r['dormitorios'] != 1 else ''}")
            else:
                if r["dormitorios"] == d["quartos"] - 1:
                    pts += 6
                falta.append(f"{d['quartos']}+ quartos")
        if d.get("vagas"):
            maxi += 5
            if r["vagas"] >= d["vagas"]:
                pts += 5; ok.append(f"{r['vagas']} vaga{'s' if r['vagas'] != 1 else ''}")
            else:
                falta.append(f"{d['vagas']}+ vagas")
        if d.get("extras"):
            maxi += 10
            carac = gerar_slug((r.get("caracteristicas") or "").replace("|", " "))
            tem = [e for e in d["extras"] if gerar_slug(e) in carac]
            pts += 10 * len(tem) / len(d["extras"]); ok += tem
            falta += [e for e in d["extras"] if e not in tem][:1]
        score = round(100 * pts / maxi) if maxi else 50
        pontuados.append((score, bool(r["destaque_efetivo"]), r, ok, falta))
    pontuados = [x for x in pontuados if x[0] >= 35]
    pontuados.sort(key=lambda x: (-x[0], not x[1]))
    exatos = sum(1 for x in pontuados if x[0] >= 90)
    cards = []
    for score, _, r, ok, falta in pontuados[:qtd]:
        specs = " · ".join(x for x in (f"{r['dormitorios']} quarto{'s' if r['dormitorios'] != 1 else ''}" if r["dormitorios"] else "",
                                       f"{r['vagas']} vaga{'s' if r['vagas'] != 1 else ''}" if r["vagas"] else "",
                                       f"{_fmt_num(r['area'])} m²" if r["area"] else "") if x)
        cards.append({"id": r["id"], "slug": r["slug"], "titulo": r["titulo"], "preco": preco_txt(r), "preco_num": float(r["preco"] or 0),
                      "url": base_url() + url_for("imovel", slug=r["slug"]), "foto": url_foto(r.get("capa"), 480),
                      "local": f"{r['bairro']} · {r['cidade']}", "specs": specs, "match": score, "ok": ok[:5], "falta": falta[:2]})
    return cards, exatos


# ---------- sessão e mensagens ----------

def _chat_ip():
    return request.remote_addr or "?"


_chat_ips = {}


def _chat_limite_ip(acao, maximo, janela=600):
    """Segura abuso: no máximo `maximo` chamadas de cada `acao` por IP a cada `janela` segundos."""
    chave, t = (_chat_ip(), acao), time.time()
    marcas = [x for x in _chat_ips.get(chave, []) if t - x < janela] + [t]
    _chat_ips[chave] = marcas
    if len(_chat_ips) > 5000:
        _chat_ips.clear()
    return len(marcas) > maximo


def _erro_json(msg, code=400):
    return jsonify({"erro": msg}), code


def _saudacao():
    h = datetime.now(TZ).hour
    return "Bom dia" if h < 12 else "Boa tarde" if h < 18 else "Boa noite"


def _tpl(txt, cfg):
    return (txt or "").replace("{nome}", cfg["nome"]).replace("{site}", SITE_NOME).replace("{saudacao}", _saudacao())


def _chat_sessao():
    sid = session.get("chat_sid")
    s = query_one("SELECT * FROM chat_sessoes WHERE id = %s", (sid,)) if sid else None
    if s:
        try:
            s["p"] = json.loads(s["perfil"] or "{}")
        except ValueError:
            s["p"] = {}
        s["p"].setdefault("dados", {}); s["p"].setdefault("ok", [])
    return s


def _perfil_salvar(sid, p):
    execute("UPDATE chat_sessoes SET perfil = %s, atualizado_em = NOW() WHERE id = %s", (json.dumps(p, ensure_ascii=False), sid))


def _msg(sid, papel, tipo, texto, **meta):
    """Salva a mensagem e devolve o dict que o widget desenha."""
    execute("INSERT INTO chat_mensagens (sessao_id, papel, tipo, texto, meta) VALUES (%s,%s,%s,%s,%s)",
            (sid, papel, tipo, texto, json.dumps(meta, ensure_ascii=False) if meta else None))
    return dict(meta, papel=papel, tipo=tipo, texto=texto)


def _msgs_sessao(sid):
    out = []
    for r in query_all("SELECT papel, tipo, texto, meta FROM chat_mensagens WHERE sessao_id = %s ORDER BY id", (sid,)):
        try:
            meta = json.loads(r["meta"]) if r["meta"] else {}
        except ValueError:
            meta = {}
        meta.pop("tokens", None); meta.pop("chave", None); meta.pop("erro", None)
        out.append(dict(meta, papel=r["papel"], tipo=r["tipo"], texto=r["texto"]))
    return out


def _passo(chave, d):
    fin = d.get("finalidade") or "venda"
    if chave == "finalidade":
        return {"chave": chave, "texto": "Você quer comprar ou alugar?", "opcoes": [{"v": "venda", "r": "Comprar"}, {"v": "aluguel", "r": "Alugar"}]}
    if chave == "tipo":
        return {"chave": chave, "texto": "Que tipo de imóvel você procura?",
                "opcoes": [{"v": k, "r": v} for k, v in TIPOS_IMOVEL.items()] + [{"v": "qualquer", "r": "Tanto faz"}]}
    if chave == "local":
        where, params = ["i.status = 'publicado'", "t.status = 'ativo'", "i.finalidade = %s"], [fin]
        if d.get("tipo"):
            where.append("i.tipo = %s"); params.append(d["tipo"])
        linhas = query_all("SELECT MIN(i.bairro) AS bairro, MIN(i.cidade) AS cidade, i.bairro_slug, i.cidade_slug, COUNT(*) AS n "
                           "FROM imoveis i JOIN tenants t ON t.id = i.tenant_id WHERE " + " AND ".join(where) +
                           " GROUP BY i.bairro_slug, i.cidade_slug ORDER BY n DESC LIMIT 8", params)
        ops = [{"v": f"loc:{r['cidade_slug']}|{r['bairro_slug']}", "r": f"{r['bairro']} · {r['cidade']}"} for r in linhas]
        return {"chave": chave, "texto": "Em qual região você quer morar? Pode escolher ou digitar um bairro/cidade.",
                "opcoes": ops + [{"v": "qualquer", "r": "Tanto faz"}], "texto_livre": True}
    if chave == "orcamento":
        return {"chave": chave, "texto": "Até quanto você pretende investir?" if fin == "venda" else "Qual o valor máximo de aluguel por mês?",
                "opcoes": [{"v": v, "r": r} for v, r in ORCAMENTOS[fin]]}
    if chave == "quartos":
        return {"chave": chave, "texto": "Quantos quartos você precisa?",
                "opcoes": [{"v": "1", "r": "1+"}, {"v": "2", "r": "2+"}, {"v": "3", "r": "3+"}, {"v": "4", "r": "4+"}, {"v": "0", "r": "Tanto faz"}]}
    if chave == "vagas":
        return {"chave": chave, "texto": "E vagas de garagem?",
                "opcoes": [{"v": "0", "r": "Não preciso"}, {"v": "1", "r": "1+"}, {"v": "2", "r": "2+"}]}
    return {"chave": "extras", "texto": "O que não pode faltar? Escolha quantos quiser.", "multi": True,
            "opcoes": [{"v": e, "r": e} for e in EXTRAS_CHAT]}


def _aplicar_resposta(chave, valor, d):
    """Valida a resposta de um passo e atualiza o perfil. Devolve o rótulo (ou None se inválida)."""
    if chave == "finalidade" and valor in ("venda", "aluguel"):
        d["finalidade"] = valor
        return "Comprar" if valor == "venda" else "Alugar"
    if chave == "tipo" and (valor in TIPOS_IMOVEL or valor == "qualquer"):
        if valor != "qualquer":
            d["tipo"] = valor
        return TIPOS_IMOVEL.get(valor, "Tanto faz")
    if chave == "local" and isinstance(valor, str):
        if valor == "qualquer":
            return "Tanto faz"
        if valor.startswith("loc:") and "|" in valor:
            cs, bs = valor[4:].split("|", 1)
            r = query_one("SELECT MIN(bairro) AS bairro, MIN(cidade) AS cidade FROM imoveis WHERE cidade_slug = %s AND bairro_slug = %s", (cs, bs))
            if r and r["bairro"]:
                d["bairro"], d["cidade"] = r["bairro"], r["cidade"]
                return f"{r['bairro']} · {r['cidade']}"
            return None
        if valor.startswith("txt:"):
            txt = sanitize_input(valor[4:])[:80]
            if len(txt) < 2:
                return None
            primeiro = txt.split(",")[0].strip()
            c = query_one("SELECT MIN(cidade) AS cidade FROM imoveis WHERE cidade_slug = %s", (gerar_slug(primeiro),))
            if c and c["cidade"]:
                d["cidade"] = c["cidade"]
            else:
                d["bairro"] = primeiro
            return txt
        return None
    if chave == "orcamento":
        rot = dict(ORCAMENTOS.get(d.get("finalidade"), ORCAMENTOS["venda"])).get(str(valor))
        if rot is None:
            return None
        if float(valor) > 0:
            d["preco_max"] = float(valor)
        return rot
    if chave in ("quartos", "vagas") and str(valor) in ("0", "1", "2", "3", "4"):
        if int(valor) > 0:
            d[chave] = int(valor)
        return "Tanto faz" if str(valor) == "0" else f"{valor}+"
    if chave == "extras" and isinstance(valor, list):
        ex = normalizar_perfil({"extras": valor}).get("extras", [])
        if ex:
            d["extras"] = ex
        return ", ".join(ex) if ex else "Nenhum em especial"
    return None


def _chat_resultado(sid, sess, cfg, texto=None, busca=None):
    """Grava as recomendações e devolve a mensagem de resultado (com os cards)."""
    d = sess["p"]["dados"]
    cards, exatos = busca or chat_buscar(d, cfg["qtd"])
    if texto is None or not cards:
        texto = (f"Achei {len(cards)} opç{'ões' if len(cards) != 1 else 'ão'} que combinam com você 🎉" if cards else
                 "Ainda não temos um imóvel que combine direitinho, mas veja o que tem na busca do site 👇")
    for c in cards:
        execute("INSERT INTO chat_recomendacoes (sessao_id, imovel_id, imovel_slug, titulo, preco, score) VALUES (%s,%s,%s,%s,%s,%s)",
                (sid, c["id"], c["slug"], c["titulo"], c["preco_num"], c["match"]))
    execute("UPDATE chat_sessoes SET finalizada = TRUE, atualizado_em = NOW() WHERE id = %s", (sid,))
    return _msg(sid, "bot", "resultado", texto, cards=cards, link_busca=link_busca(d), resumo=resumo_perfil(d))


# ---------- rotas do widget ----------

@app.route("/chat/api/estado")
def chat_api_estado():
    cfg = chat_cfg()
    if cfg["ativo"] != "1":
        return jsonify({"ativo": False})
    j = {"ativo": True, "nome": cfg["nome"], "ia": bool(_groq_chaves()), "boas_vindas": _tpl(cfg["boas_vindas"], cfg)}
    s = _chat_sessao()
    if s:
        ms = _msgs_sessao(s["id"])
        if ms:
            j.update(mensagens=ms, modo=s["modo"], finalizada=s["finalizada"])
    return jsonify(j)


@app.route("/chat/api/iniciar", methods=["POST"])
def chat_api_iniciar():
    cfg = chat_cfg()
    if cfg["ativo"] != "1":
        return _erro_json("Chat indisponível.", 404)
    if _chat_limite_ip("iniciar", 30):
        return _erro_json("Muitas tentativas. Aguarde um pouco.", 429)
    corpo = request.get_json(silent=True) or {}
    modo = "ia" if corpo.get("modo") == "ia" else "formulario"
    if modo == "ia" and not _groq_chaves():
        return _erro_json("A conversa com IA não está disponível agora. Use o formulário rápido.", 400)
    sid = uuid.uuid4().hex[:16]
    ip_hash = hashlib.sha256((_chat_ip() + str(app.secret_key)).encode()).hexdigest()[:16]
    execute("INSERT INTO chat_sessoes (id, modo, usuario_id, perfil, ip_hash, user_agent, origem) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (sid, modo, g.usuario["id"] if g.usuario else None, json.dumps({"dados": {}, "ok": []}), ip_hash,
             (request.headers.get("User-Agent") or "")[:200], sanitize_input(corpo.get("origem") or "")[:200] or None))
    session["chat_sid"] = sid
    if modo == "ia":
        return jsonify({"mensagens": [_msg(sid, "bot", "texto", _tpl(cfg["ia_abertura"], cfg))]})
    p = _passo("finalidade", {})
    return jsonify({"mensagens": [_msg(sid, "bot", "pergunta", p["texto"], passo=p)]})


@app.route("/chat/api/reiniciar", methods=["POST"])
def chat_api_reiniciar():
    session.pop("chat_sid", None)       # a conversa antiga continua salva no banco
    return jsonify({"ok": True})


@app.route("/chat/api/formulario", methods=["POST"])
def chat_api_formulario():
    cfg = chat_cfg()
    if cfg["ativo"] != "1":
        return _erro_json("Chat indisponível.", 404)
    if _chat_limite_ip("form", 120):
        return _erro_json("Muitas mensagens seguidas. Aguarde um pouco.", 429)
    s = _chat_sessao()
    if not s or s["modo"] != "formulario":
        return _erro_json("Conversa não encontrada. Comece de novo.", 404)
    corpo = request.get_json(silent=True) or {}
    esperado = next((x for x in PASSOS if x not in s["p"]["ok"]), None)
    if esperado is None or corpo.get("passo") != esperado:
        return _erro_json("Essa pergunta já foi respondida.", 409)
    d = s["p"]["dados"]
    rotulo = _aplicar_resposta(esperado, corpo.get("valor"), d)
    if rotulo is None:
        return _erro_json("Resposta inválida. Escolha uma das opções.", 400)
    s["p"]["ok"].append(esperado)
    valor = corpo.get("valor")
    execute("INSERT INTO chat_respostas (sessao_id, campo, valor, rotulo) VALUES (%s,%s,%s,%s)",
            (s["id"], esperado, json.dumps(valor, ensure_ascii=False) if isinstance(valor, list) else str(valor), rotulo))
    _msg(s["id"], "user", "texto", rotulo)
    prox = next((x for x in PASSOS if x not in s["p"]["ok"]), None)
    if prox:
        _perfil_salvar(s["id"], s["p"])
        p = _passo(prox, d)
        return jsonify({"mensagens": [_msg(s["id"], "bot", "pergunta", p["texto"], passo=p)]})
    _perfil_salvar(s["id"], s["p"])
    return jsonify({"mensagens": [_chat_resultado(s["id"], s, cfg)]})


def _system_ia(cfg, dados):
    persona = _tpl(cfg["persona"], cfg)
    regras = (
        "\n\nREGRAS FIXAS (valem sempre):\n"
        "- Responda SOMENTE com um objeto JSON, sem texto fora dele, neste formato: "
        '{"resposta": "texto curto para a pessoa", "perfil": {"finalidade": "venda|aluguel|null", "tipo": "apartamento|casa|cobertura|comercial|terreno|null", '
        '"bairro": "texto|null", "cidade": "texto|null", "preco_max": numero_em_reais_ou_null, "quartos": numero_ou_null, "vagas": numero_ou_null, '
        '"extras": ["Piscina","Sacada","Churrasqueira","Elevador","Aceita pets","Mobiliado"]}, "pronto": true|false}.\n'
        "- Em \"perfil\" coloque tudo o que já sabe da pessoa (acumulado), usando null no que ainda não sabe.\n"
        "- \"pronto\" = true só quando já souber finalidade, tipo e (região ou orçamento), ou quando a pessoa pedir para ver as opções. "
        "Quando pronto for true, a resposta deve ser só um aviso curto de que vai buscar.\n"
        "- NUNCA invente imóveis, preços, endereços ou links. Quem mostra os imóveis é o sistema, depois que você marcar pronto = true.\n"
        "- TODA resposta (enquanto pronto for false) TERMINA com UMA pergunta curta sobre o próximo dado que falta, nesta ordem: "
        "finalidade, tipo, bairro/cidade, orçamento, quartos, vagas. Nunca responda só com uma confirmação.\n"
        "- Se a pessoa falar de outro assunto, responda em uma frase e volte para a busca do imóvel.\n"
        f"- Hoje é {_saudacao().lower()} no horário da pessoa.\n"
        f"- O que já se sabe do perfil: {json.dumps(dados, ensure_ascii=False) if dados else 'nada ainda'}")
    return persona + regras


def _fechamento_ia(cfg, dados, cards):
    """Frase final humana apresentando as opções (a lista em si vem do banco, nunca da IA)."""
    padrao = "Separei essas opções pra você, olha só 🏡"
    if not cards:
        return ""
    try:
        lista = "\n".join(f"- {c['titulo']} ({c['preco']}), {c['local']}, {c['match']}% de compatibilidade" for c in cards)
        txt, _, _ = groq_chat([
            {"role": "system", "content": _tpl(cfg["persona"], cfg) + "\n\nAgora apresente, em 1 ou 2 frases curtas e sem listas, as opções que o sistema encontrou. "
             "Cite só o que está na lista abaixo, sem inventar nada. Não repita os preços."},
            {"role": "user", "content": f"Perfil: {resumo_perfil(dados)}\nOpções encontradas:\n{lista}"}], cfg["temp"])
        return sanitize_input(txt)[:400] or padrao
    except Exception as e:
        log.warning("fechamento IA: %s", e)
        return padrao


@app.route("/chat/api/ia", methods=["POST"])
def chat_api_ia():
    cfg = chat_cfg()
    if cfg["ativo"] != "1":
        return _erro_json("Chat indisponível.", 404)
    if _chat_limite_ip("ia", 60):
        return _erro_json("Muitas mensagens seguidas. Aguarde um pouco.", 429)
    s = _chat_sessao()
    if not s or s["modo"] != "ia":
        return _erro_json("Conversa não encontrada. Comece de novo.", 404)
    texto = sanitize_input((request.get_json(silent=True) or {}).get("texto", ""))[:500]
    if not texto:
        return _erro_json("Escreva alguma coisa.", 400)
    sid = s["id"]
    n_user = query_one("SELECT COUNT(*) AS n FROM chat_mensagens WHERE sessao_id = %s AND papel = 'user'", (sid,))["n"]
    _msg(sid, "user", "texto", texto)
    if n_user >= cfg["max_msgs_n"]:
        return jsonify({"mensagens": [_msg(sid, "bot", "erro", "Chegamos ao limite desta conversa. Que tal ver as opções pelo formulário rápido?", acao="formulario")]})
    hist = query_all("SELECT papel, texto FROM chat_mensagens WHERE sessao_id = %s AND tipo IN ('texto','resultado') ORDER BY id DESC LIMIT 14", (sid,))[::-1]
    msgs = [{"role": "system", "content": _system_ia(cfg, s["p"]["dados"])}] + \
           [{"role": "user" if h["papel"] == "user" else "assistant", "content": h["texto"] or ""} for h in hist]
    try:
        bruto, tokens, chave = groq_chat(msgs, cfg["temp"], json_mode=True)
    except Exception as e:
        log.error("chat IA indisponível: %s", e)
        return jsonify({"mensagens": [_msg(sid, "bot", "erro", "Eita, fiquei sem sinal por um instante 😅 Tenta de novo ou responde as perguntas rápidas.",
                                           acao="formulario", erro=str(e)[:300])]})
    try:
        j = _json_da_ia(bruto)
    except ValueError:
        j = {"resposta": sanitize_input(re.sub(r"[{}\[\]\"]", "", bruto))[:300], "perfil": {}, "pronto": False}
    resposta = sanitize_input(j.get("resposta") if isinstance(j.get("resposta"), str) else "")[:500] or "Me conta mais sobre o que você procura?"
    d = s["p"]["dados"]
    novo = normalizar_perfil(j.get("perfil"))
    d.update(novo)
    _perfil_salvar(sid, s["p"])
    if j.get("pronto") is not True and "?" not in resposta:      # a IA esqueceu de perguntar: o sistema pergunta o que falta
        falta = next((q for k, q in (("finalidade", "Você quer comprar ou alugar?"), ("tipo", "Que tipo de imóvel você procura?"),
                                     ("local", "Em qual bairro ou cidade?"), ("preco_max", "Até quanto você pretende investir?"),
                                     ("quartos", "Quantos quartos você precisa?")) if not (d.get("bairro") or d.get("cidade")) and k == "local"
                      or k not in ("local",) and not d.get(k)), None)
        if falta:
            resposta = (resposta + " " + falta)[:600]
    saida = [_msg(sid, "bot", "texto", resposta, tokens=tokens, chave=chave)]
    if j.get("pronto") is True and (d.get("finalidade") or d.get("tipo") or d.get("bairro") or d.get("cidade")):
        busca = chat_buscar(d, cfg["qtd"])
        saida.append(_chat_resultado(sid, s, cfg, _fechamento_ia(cfg, d, busca[0]) or None, busca))
    return jsonify({"mensagens": saida})


# ---------- admin do chatbot ----------

def _chaves_mascaradas():
    return ["…" + k[-4:] for k in _groq_chaves()]


@app.route("/admin/chatbot")
@admin_required
def admin_chatbot():
    aba = "conversas" if request.args.get("aba") == "conversas" else "config"
    ctx = dict(pagina_ativa="chatbot", chat_aba=aba)
    if aba == "conversas":
        modo = request.args.get("modo") if request.args.get("modo") in ("formulario", "ia") else ""
        so_conc = request.args.get("so_concluidas") == "1"
        where, params = ["TRUE"], []
        if modo:
            where.append("s.modo = %s"); params.append(modo)
        if so_conc:
            where.append("s.finalizada")
        lista = query_all(
            "SELECT s.id, s.modo, s.criado_em, s.perfil, u.nome AS usuario_nome, "
            "(SELECT COUNT(*) FROM chat_mensagens m WHERE m.sessao_id = s.id) AS n_msgs, "
            "(SELECT COUNT(*) FROM chat_recomendacoes r WHERE r.sessao_id = s.id) AS n_rec "
            "FROM chat_sessoes s LEFT JOIN usuarios u ON u.id = s.usuario_id WHERE " + " AND ".join(where) +
            " ORDER BY s.criado_em DESC LIMIT 100", params)
        for s in lista:
            try:
                s["resumo"] = resumo_perfil((json.loads(s["perfil"] or "{}")).get("dados") or {})
            except ValueError:
                s["resumo"] = ""
        stats = query_one("SELECT (SELECT COUNT(*) FROM chat_sessoes) AS total, (SELECT COUNT(*) FROM chat_mensagens) AS msgs, "
                          "(SELECT COUNT(*) FROM chat_sessoes WHERE finalizada) AS concluidas, "
                          "(SELECT COUNT(*) FROM chat_sessoes WHERE modo = 'formulario') AS form, "
                          "(SELECT COUNT(*) FROM chat_sessoes WHERE modo = 'ia') AS ia")
        ctx.update(chat_lista=lista, chat_stats=stats, chat_modo=modo, chat_so_concluidas=so_conc)
    else:
        hist = query_all("SELECT h.id, h.valor, h.criado_em, u.nome AS quem FROM chatbot_config_historico h "
                         "LEFT JOIN usuarios u ON u.id = h.usuario_id WHERE h.chave = 'persona' ORDER BY h.id DESC LIMIT 15")
        ctx.update(cfg=chat_cfg(forcar=True), chat_chaves=_chaves_mascaradas(),
                   chat_modelo=GROQ_MODELO, chat_hist=hist)
    return render_template("admin.html", **ctx)


@app.route("/admin/chatbot/conversa/<sid>")
@admin_required
def admin_chatbot_conversa(sid):
    s = query_one("SELECT * FROM chat_sessoes WHERE id = %s", (sid,))
    if not s:
        abort(404)
    msgs = query_all("SELECT * FROM chat_mensagens WHERE sessao_id = %s ORDER BY id", (sid,))
    for m in msgs:
        try:
            m["m"] = json.loads(m["meta"]) if m["meta"] else {}
        except ValueError:
            m["m"] = {}
    try:
        dados = (json.loads(s["perfil"] or "{}")).get("dados") or {}
    except ValueError:
        dados = {}
    return render_template("admin.html", pagina_ativa="chatbot", chat_aba="conversa", chat_s=s, chat_msgs=msgs, chat_resumo=resumo_perfil(dados),
                           chat_resp=query_all("SELECT campo, rotulo FROM chat_respostas WHERE sessao_id = %s ORDER BY id", (sid,)),
                           chat_rec=query_all("SELECT imovel_slug, titulo, preco, score FROM chat_recomendacoes WHERE sessao_id = %s ORDER BY id", (sid,)))


@app.route("/admin/chatbot/salvar", methods=["POST"])
@admin_required
def admin_chatbot_salvar():
    f = request.form
    uid = g.usuario["id"]
    try:
        temp = min(1.5, max(0.0, float(f.get("temperatura", "0.6").replace(",", "."))))
    except ValueError:
        temp = 0.6
    def _n(nome, mn, mx, pad):
        try:
            return str(min(mx, max(mn, int(f.get(nome, pad)))))
        except ValueError:
            return str(pad)
    chat_cfg_set("ativo", "1" if f.get("ativo") else "0")
    chat_cfg_set("nome", sanitize_input(f.get("nome", ""))[:30] or "Duda")
    chat_cfg_set("temperatura", f"{temp:g}")
    chat_cfg_set("qtd_resultados", _n("qtd_resultados", 1, 5, 3))
    chat_cfg_set("max_msgs", _n("max_msgs", 6, 200, 40))
    for chave, lim in (("boas_vindas", 400), ("ia_abertura", 400)):
        if f.get(chave) is not None:
            chat_cfg_set(chave, sanitize_input(f[chave])[:lim] or CHAT_PADRAO[chave])
    persona = (f.get("persona") or "").replace("\x00", "").strip()[:4000]
    chat_cfg_set("persona", persona or PERSONA_PADRAO, uid)
    _chat_cfg_cache["v"] = None
    flash("Configurações do chatbot salvas.", "success")
    return redirect(url_for("admin_chatbot"))


@app.route("/admin/chatbot/restaurar", methods=["POST"])
@admin_required
def admin_chatbot_restaurar():
    hid = request.form.get("hist_id", type=int)
    if hid:
        h = query_one("SELECT valor FROM chatbot_config_historico WHERE id = %s AND chave = 'persona'", (hid,))
        if not h:
            flash("Versão não encontrada.", "error")
            return redirect(url_for("admin_chatbot"))
        chat_cfg_set("persona", h["valor"], g.usuario["id"])
    else:
        chat_cfg_set("persona", PERSONA_PADRAO, g.usuario["id"])
    _chat_cfg_cache["v"] = None
    flash("Persona restaurada. A versão anterior continua no histórico.", "success")
    return redirect(url_for("admin_chatbot"))


@app.route("/admin/chatbot/testar", methods=["POST"])
@admin_required
def admin_chatbot_testar():
    chaves = _groq_chaves()
    if not chaves:
        flash("Nenhuma chave Groq no .env (GROQ_API_KEY).", "error")
    for n, k in enumerate(chaves, 1):
        t0 = time.time()
        try:
            r = _groq_post(k, _groq_payload([{"role": "user", "content": "Responda apenas: ok"}], 0, False, 200))
            if r.status_code == 200:
                flash(f"Chave {n} (…{k[-4:]}): funcionando — {time.time() - t0:.1f}s com {GROQ_MODELO}.", "success")
            else:
                try:
                    det = (r.json().get("error") or {}).get("message", "")
                except Exception:
                    det = ""
                flash(f"Chave {n} (…{k[-4:]}): HTTP {r.status_code}: {det[:120]}", "error")
        except Exception as e:
            flash(f"Chave {n} (…{k[-4:]}): {type(e).__name__}", "error")
    return redirect(url_for("admin_chatbot"))


# ═══════════════════════════════════════════════════════════════
# 7. DADOS INICIAIS (admin + demonstração) e inicialização
# ═══════════════════════════════════════════════════════════════

def _imagem_demo(semente, tamanho=(1200, 800)):
    """Gera uma 'foto' de fachada colorida (só para a demonstração funcionar sem internet)."""
    import random
    rnd = random.Random(semente)
    w, h = tamanho
    topo, base = rnd.choice([((135, 190, 235), (225, 240, 250)), ((250, 190, 140), (255, 230, 200)),
                             ((120, 170, 210), (210, 230, 240)), ((170, 150, 210), (235, 225, 245))])
    img = Image.new("RGB", tamanho)
    d = ImageDraw.Draw(img)
    for y in range(h):
        k = y / h
        d.line([(0, y), (w, y)], fill=tuple(int(topo[c] + (base[c] - topo[c]) * k) for c in range(3)))
    d.rectangle([0, int(h * .78), w, h], fill=(96, 160, 96))
    cor = rnd.choice([(236, 230, 220), (214, 200, 184), (190, 205, 215), (230, 215, 200)])
    if rnd.random() < .5:   # prédio
        x0, x1, y0 = int(w * .3), int(w * .7), int(h * .12)
        d.rectangle([x0, y0, x1, int(h * .8)], fill=cor)
        for r in range(6):
            for c in range(4):
                x, y = x0 + 40 + c * ((x1 - x0 - 60) // 4), y0 + 40 + r * 92
                d.rectangle([x, y, x + 52, y + 52], fill=(70, 110, 140) if rnd.random() > .2 else (250, 225, 130))
    else:                   # casa
        d.rectangle([int(w * .22), int(h * .42), int(w * .78), int(h * .8)], fill=cor)
        d.polygon([(int(w * .18), int(h * .42)), (int(w * .5), int(h * .2)), (int(w * .82), int(h * .42))], fill=(150, 80, 70))
        d.rectangle([int(w * .46), int(h * .55), int(w * .54), int(h * .8)], fill=(110, 80, 60))
        for x in (.3, .62):
            d.rectangle([int(w * x), int(h * .52), int(w * (x + .1)), int(h * .66)], fill=(80, 120, 150))
    return img


def garantir_admin():
    if ADMIN_EMAIL and ADMIN_SENHA and not query_one("SELECT 1 FROM usuarios WHERE tipo = 'admin'"):
        if not query_one("SELECT 1 FROM usuarios WHERE email = %s", (ADMIN_EMAIL,)):
            criar_usuario("Administrador", ADMIN_EMAIL, ADMIN_SENHA, "admin")
            log.info("Admin criado: %s", ADMIN_EMAIL)


DEMO = [  # (tenant, titulo, finalidade, tipo, preco, bairro, dorm, vagas, area, lat, lng, status, destaque)
    ("vieira", "Apartamento moderno na Vila Mariana", "venda", "apartamento", 850000, "Vila Mariana", 3, 2, 98, -23.5890, -46.6340, "publicado", True),
    ("vieira", "Casa com jardim e área gourmet", "venda", "casa", 1240000, "Alto da Lapa", 4, 3, 184, -23.5260, -46.7050, "publicado", True),
    ("vieira", "Apartamento próximo ao metrô", "aluguel", "apartamento", 4800, "Paraíso", 2, 1, 76, -23.5750, -46.6430, "publicado", False),
    ("vieira", "Sobrado com excelente localização", "venda", "casa", 720000, "Tatuapé", 3, 2, 132, -23.5400, -46.5760, "publicado", False),
    ("vieira", "Cobertura duplex com terraço", "venda", "cobertura", 2350000, "Moema", 4, 3, 210, -23.6010, -46.6660, "pendente", False),
    ("exemplo", "Apartamento compacto em Pinheiros", "venda", "apartamento", 620000, "Pinheiros", 2, 1, 65, -23.5670, -46.6920, "publicado", False),
    ("exemplo", "Apartamento amplo próximo ao parque", "venda", "apartamento", 1090000, "Moema", 3, 2, 112, -23.5990, -46.6640, "publicado", True),
    ("exemplo", "Apartamento novo e mobiliado", "aluguel", "apartamento", 3200, "Saúde", 1, 1, 45, -23.6120, -46.6360, "publicado", False),
    ("exemplo", "Casa com piscina e 4 quartos", "aluguel", "casa", 4500, "Vila Mariana", 4, 3, 250, -23.5850, -46.6300, "publicado", False),
    ("mariana", "Apartamento reformado e iluminado", "venda", "apartamento", 780000, "Paraíso", 2, 2, 82, -23.5740, -46.6420, "publicado", False),
    ("mariana", "Sala comercial com vaga", "aluguel", "comercial", 2900, "Pinheiros", 0, 1, 40, -23.5650, -46.6880, "publicado", False),
]


def seed_demo():
    if query_one("SELECT 1 FROM usuarios WHERE email = 'visitante@demo.com'"):
        return
    log.info("Criando dados de demonstração…")
    criar_usuario("Mariana Costa", "visitante@demo.com", "demo1234", "visitante", cidade="São Paulo")
    ids = {}
    for chave, nome, email, tipo, plano in (("vieira", "Vieira Imóveis", "vieira@demo.com", "imobiliaria", "profissional"),
                                            ("exemplo", "Imobiliária Exemplo", "exemplo@demo.com", "imobiliaria", "gratis"),
                                            ("mariana", "Carla Souza Corretora", "corretora@demo.com", "corretor", "gratis")):
        uid = criar_usuario(nome, email, "demo1234", tipo, "11999999999", "São Paulo")
        t = query_one("SELECT id FROM tenants WHERE usuario_id = %s", (uid,))
        ids[chave] = t["id"]
        execute("UPDATE tenants SET uf='SP', creci='12345-J', horario='Seg a Sex — 09h às 18h', verificada = %s, "
                "descricao='Especialistas em imóveis residenciais em São Paulo. Atendimento próximo e personalizado.', "
                "anos_mercado = 8 WHERE id = %s", (chave != "mariana", t["id"]))
        if plano == "profissional":
            execute("UPDATE tenants SET cor_primaria = '#1e90ff' WHERE id = %s", (t["id"],))
            ativar_profissional(t["id"], 30, provedor="demo")
    for n, (chave, titulo, fin, tipo, preco, bairro, dorm, vagas, area, lat, lng, status, dest) in enumerate(DEMO):
        iid = execute_returning(
            "INSERT INTO imoveis (tenant_id, titulo, slug, finalidade, tipo, preco, condominio, iptu, cidade, cidade_slug, uf, bairro, "
            "bairro_slug, dormitorios, suites, banheiros, vagas, area, descricao, caracteristicas, lat, lng, status, destaque) "
            "VALUES (%s,%s,%s,%s,%s,%s,980,240,'São Paulo','sao-paulo','SP',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (ids[chave], titulo, slug_unico("imoveis", gerar_slug(titulo)), fin, tipo, preco, bairro, gerar_slug(bairro), dorm,
             1 if dorm >= 3 else 0, max(1, dorm - 1), vagas, area,
             "Imóvel espaçoso e bem localizado, com ambientes integrados, boa iluminação natural e acabamento moderno. "
             "Próximo a restaurantes, mercados, escolas e transporte público.",
             "|".join(CARACTERISTICAS[:6] if n % 2 == 0 else CARACTERISTICAS[3:9]), lat, lng, status, dest))
        for k in range(3):
            fid = storage.salvar_imagem(_imagem_demo(n * 10 + k), f"demo-{n}-{k}.jpg", "demo")
            execute("INSERT INTO imovel_fotos (imovel_id, file_id, ordem) VALUES (%s,%s,%s)", (iid, fid, k))
        for cat, nome, dist in (("metro", "Estação de metrô", 350), ("mercado", "Supermercado", 200), ("escola", "Escola", 600)):
            execute("INSERT INTO imovel_proximos (imovel_id, categoria, nome, link, distancia_m, status) VALUES (%s,%s,%s,%s,%s,'aprovado')",
                    (iid, cat, nome, "https://www.google.com/maps/search/" + quote(nome), dist))
        if status == "publicado":
            for _ in range(5 + n * 3):
                execute("INSERT INTO eventos (tenant_id, imovel_id, tipo, criado_em) VALUES (%s,%s,'view', NOW() - (random() * 30) * INTERVAL '1 day')",
                        (ids[chave], iid))
            vid = query_one("SELECT id FROM usuarios WHERE email = 'visitante@demo.com'")["id"]
            if n % 3 == 0:
                execute("INSERT INTO eventos (tenant_id, imovel_id, usuario_id, tipo) VALUES (%s,%s,%s,'whatsapp')", (ids[chave], iid, vid))
                execute("INSERT INTO favoritos (usuario_id, imovel_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (vid, iid))
            if n == 0:
                execute("INSERT INTO eventos (tenant_id, imovel_id, usuario_id, tipo, detalhe) VALUES (%s,%s,%s,'visita','08/10 às 15:30')",
                        (ids[chave], iid, vid))
    execute("INSERT INTO imovel_proximos (imovel_id, categoria, nome, status) SELECT id, 'parque', 'Parque do bairro (sem link)', 'pendente' "
            "FROM imoveis WHERE slug LIKE 'apartamento-moderno%%' LIMIT 1")


def _bootstrap():
    init_db()
    with app.app_context():
        execute("SELECT pg_advisory_xact_lock(727275)")   # vários workers → só um popula
        garantir_admin()
        if SEED_DEMO:
            seed_demo()


# Minha Casa Minha Vida: módulo separado (mcmv.py, na mesma pasta). Se faltar ou der erro, o resto do site segue normal.
try:
    import mcmv
    mcmv.init(app, query_all=query_all, query_one=query_one, preparar=preparar_imoveis,
              capa_sql=CAPA_SQL, gerar_slug=gerar_slug, base_url=base_url)
except Exception:
    log.exception("MCMV: o módulo não carregou (o resto do site segue normal)")

if DATABASE_URL:
    _bootstrap()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=_bool("DEBUG"))
