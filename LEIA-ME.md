# Painel do Personagem — Twitch Extension (Warmane)

Panel Extension que mostra **Ashokemon @ Icecrown** com o modelo girando,
os itens equipados em volta e os atributos de Warlock embaixo — usando
o `PAINEL.png` como moldura.

```
extensao-warmane/
├── panel.html        ← o painel (é isso que aparece na Twitch)
├── config.html       ← página de config exigida pela Twitch
├── assets/
│   └── PAINEL.png
├── fonts/            ← coloque a Friz Quadrata aqui
├── gerar_dados.py    ← roda no seu PC e alimenta o painel
└── dados/            ← o que o script gera; é isso que você publica
```

---

## Como funciona (a arquitetura em uma frase)

O `gerar_dados.py` roda no **seu PC**, captura o modelo e os dados da
Armory, e cospe uma pasta `dados/`. Você publica essa pasta em qualquer
host estático. A Extension, rodando dentro da Twitch, só lê essa pasta.

```
seu PC (gerar_dados.py)  →  host estático (dados/)  →  Twitch (panel.html)
```

A Extension **não** captura nada sozinha — ela não pode, é um iframe
sandboxado. Quem captura é o script no seu PC.

---

## Passo 1 — Gerar os dados

```
pip install playwright requests
playwright install chromium
python gerar_dados.py
```

Isso cria:

```
dados/
├── dados.json          ← nome, nível, guilda, itens, atributos
├── modelo_00.png ... modelo_11.png   ← o personagem girando
└── icones/             ← ícone de cada item equipado
```

Opções úteis:

| Comando | O que faz |
|---|---|
| `--frames 16` | mais frames = rotação mais suave (e mais peso) |
| `--frames 1` | imagem parada, sem animação |
| `--nome X --realm Y` | outro personagem |
| `--debug` | abre o navegador visível, pra ver o que está acontecendo |

**Se a rotação sair torta ou o modelo não aparecer**, rode com `--debug`
e observe. O script arrasta o mouse sobre o canvas pra girar o modelo;
dependendo de como a Armory reagir, pode ser preciso ajustar o
`ESPERA_MODELO_S` ou o multiplicador de `passo` dentro de
`gira_e_captura()`.

---

## Passo 2 — Publicar a pasta `dados/`

Precisa ser um endereço público em **HTTPS**. O caminho mais simples e
gratuito é o GitHub Pages:

1. Crie um repositório público (ex: `warmane-painel`)
2. Suba o conteúdo da pasta `dados/` na raiz dele
3. Settings → Pages → Source: `main` / `root` → Save
4. Sua URL vira algo como `https://seuusuario.github.io/warmane-painel/`

Serve qualquer outro host estático: Cloudflare Pages, Netlify, Vercel.

---

## Passo 3 — Apontar o painel pra essa URL

Abra o `panel.html` num editor e preencha, no topo:

```js
BASE_DADOS: 'https://seuusuario.github.io/warmane-painel/',
```

Precisa terminar com barra. Se deixar vazio, o painel roda em **modo
demonstração** com dados de exemplo — bom pra ver o visual antes de
publicar qualquer coisa.

---

## Passo 4 — A fonte Friz Quadrata

É a fonte do WoW, e é **comercial** — por isso não vem junto aqui. Se
você tiver o arquivo, coloque em `fonts/` com um destes nomes e ela é
usada automaticamente:

```
fonts/friz-quadrata.woff2   (melhor)
fonts/friz-quadrata.woff
fonts/friz-quadrata.ttf
fonts/friz-quadrata.otf
```

Sem ela, o painel cai no fallback (Cinzel → Georgia → serif) e continua
funcionando normalmente. Alternativas gratuitas com ar parecido:
**Cinzel**, **Grenze**, **IM Fell English**.

---

## Passo 5 — Criar a Extension na Twitch

1. Entre em [dev.twitch.tv/console/extensions](https://dev.twitch.tv/console/extensions) e clique em **Create Extension**
2. Tipo: marque **Panel**
3. Em **Version Details**, preencha:

| Campo | Valor |
|---|---|
| Panel Height | **500** |
| Panel Viewer Path | `panel.html` |
| Config Path | `config.html` |
| Testing Base URI | (só se for testar local) |

4. Em **Asset Hosting**, escolha hospedar na Twitch e suba um `.zip` com:

```
panel.html
config.html
assets/PAINEL.png
fonts/...          (se tiver a fonte)
```

Não inclua `gerar_dados.py` nem a pasta `dados/` no zip — eles não rodam
dentro da Twitch.

5. **Capabilities → Allowlist** — este passo é obrigatório, senão o
painel não carrega nada:

| Campo | O que colocar |
|---|---|
| Allowlist for URL Fetching Domains (`connect-src`) | o domínio do seu host, ex: `seuusuario.github.io` |
| Allowlist for Image Domains (`img-src`) | o mesmo domínio |

Como o `gerar_dados.py` baixa os ícones pra dentro da pasta `dados/`,
você só precisa declarar **um domínio** — não precisa liberar o Wowhead
nem o Warmane.

6. **Status → Move to Local Test** → **View on Twitch and Install** →
**Activate** → escolher um slot de painel

Pronto: já aparece no seu canal. **Sem revisão, sem espera** — revisão
só existe se um dia você quiser publicar pra outros streamers usarem.

---

## Passo 6 — Deixar atualizando sozinho

Agende o `gerar_dados.py` pra rodar de tempos em tempos e dar push nos
arquivos novos. No Windows, Agendador de Tarefas; um `.bat` assim
resolve:

```bat
@echo off
cd /d C:\caminho\para\extensao-warmane
python gerar_dados.py
cd dados
git add -A
git commit -m "atualiza personagem"
git push
```

O painel rebusca o `dados.json` sozinho a cada 10 minutos
(`ATUALIZAR_A_CADA_MIN` no topo do `panel.html`), então depois disso não
tem mais nada pra você apertar.

---

## O que o painel mostra

- **Topo:** nome, nível, raça, classe e guilda
- **Meio:** modelo girando em loop, com os 19 slots de equipamento em
  volta (8 à esquerda, 8 à direita, 3 armas embaixo) — no layout da
  ficha de personagem do próprio jogo
- **Faixa:** passando o mouse (ou tocando, no celular) num item, o nome
  aparece colorido pela raridade
- **Embaixo:** Poder Mágico, Acerto, Crítico e Aceleração em destaque,
  mais Intelecto, Vigor, Espírito e Armadura
- **Rodapé:** online/offline, realm e hora da última atualização

---

## Detalhes que valem saber

**A rotação é pseudo-3D, não 3D de verdade.** São fotos do modelo em
ângulos diferentes, passando em loop. 3D ao vivo dentro da Extension
esbarra nas regras da Twitch: todo JS externo tem que vir empacotado no
upload e iframes embutidos não são permitidos — e a biblioteca de modelo
do Wowhead depende de buscar dados em runtime. Na prática, os frames
entregam quase o mesmo efeito sem brigar com a plataforma.

**Os atributos vêm da página, não da API.** A API pública do Warmane
devolve nome, nível, guilda, conquistas e equipamento, mas **não**
devolve stats — Poder Mágico, Acerto, Crítico e afins só existem no HTML
do perfil. Por isso o script lê a página com o navegador em vez de só
chamar a API.

**Se o Crítico aparecer como 0%**, não é bug do painel: a própria Armory
do Warmane devolve `Critical: 0%` na seção Spell para vários
personagens. Confira no site deles pra comparar.

**A API do Warmane não é oficial.** É uma funcionalidade que a equipe
deles documentou no fórum, e pode mudar sem aviso. Se o script parar de
funcionar do nada, provavelmente foi isso.

---

## Se algo não funcionar

| Sintoma | Causa provável |
|---|---|
| Painel mostra "Não consegui carregar os dados" | `BASE_DADOS` errado, ou o domínio não foi declarado na Allowlist |
| Itens sem ícone | a pasta `icones/` não subiu junto com o `dados.json` |
| Modelo não aparece | o `gerar_dados.py` não achou o canvas — rode com `--debug` |
| Fontes com cara errada | falta o arquivo em `fonts/` (está usando o fallback) |
| Mudei o `panel.html` e nada mudou | precisa subir o `.zip` de novo em **Files** |
