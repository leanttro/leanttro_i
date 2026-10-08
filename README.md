# ImoveLonde
Rodar local: `pip install -r requirements.txt` e `python app.py` → http://localhost:8000 (defina DATABASE_URL no .env)

Logins de demonstração (SEED_DEMO=1):
- Admin: admin@demo.com / admin1234  → /admin
- Imobiliária (Profissional): vieira@demo.com / demo1234
- Imobiliária (Grátis): exemplo@demo.com / demo1234
- Visitante: visitante@demo.com / demo1234

Produção: use seu Postgres em `DATABASE_URL` (.env), monte um volume em `/data/uploads` (fotos) e deixe SEED_DEMO=0.
Tudo do backend está no `app.py`; cada página em `templates/` é um HTML único com CSS e JS embutidos.
