#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gerar_dados.py
==============
Alimenta a Twitch Extension. Faz tudo sozinho:

  1. Abre um Chromium e, com ele, busca a API de equipamento do Warmane
     (pelo navegador, não por requests puro — veja o comentário em
     busca_api_via_navegador() sobre por quê)
  2. Abre o perfil do personagem na Armory, no mesmo navegador
  3. Captura o modelo 3D girando, em N frames (vira a animação do painel)
  4. Lê os atributos direto da página
  5. Extrai ícone (e, quando dá, qualidade) de cada item direto do
     HTML da própria Armory — nada de Wowhead nem cavernoftime, porque
     um servidor privado pode ter itens diferentes do banco "oficial"
  6. Baixa os ícones pra pasta local (assim o painel só depende de 1 domínio)
  7. Escreve dados.json

Você publica a pasta 'dados/' em qualquer host estático (GitHub Pages,
Cloudflare Pages, Netlify...) e aponta o BASE_DADOS do panel.html pra lá.

USO
---
    python gerar_dados.py

    # ou sobrescrevendo o padrão:
    python gerar_dados.py --nome Ashokemon --realm Icecrown --frames 16

    # pra ver o navegador trabalhando (útil se algo der errado):
    python gerar_dados.py --debug

INSTALAÇÃO (uma vez só)
-----------------------
    pip install playwright requests pillow
    playwright install chromium
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Falta o 'requests'. Rode: pip install requests")

try:
    from PIL import Image, ImageChops   # usado pra calibrar a rotação
except ImportError:
    sys.exit("Falta o Pillow. Rode: pip install pillow")

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Falta o Playwright. Rode:\n  pip install playwright\n  playwright install chromium")


# ------------------------------------------------------------------
# Padrões — mude aqui ou passe por argumento
# ------------------------------------------------------------------
NOME_PADRAO  = "Ashokemon"
REALM_PADRAO = "Icecrown"
FRAMES_PADRAO = 12          # quantas fotos ao redor do personagem
SAIDA_PADRAO  = "dados"

URL_PERFIL = "https://armory.warmane.com/character/{nome}/{realm}/profile"
URL_API    = "https://armory.warmane.com/api/character/{nome}/{realm}/summary"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

ESPERA_MODELO_S = 9         # tempo pro WebGL carregar o modelo
TOLERANCIA_FUNDO = 8        # quão parecido com o fundo pra virar transparente
                            # (ver --tolerancia-fundo no --help pra ajustar)


# ------------------------------------------------------------------
# 1) Equipamento, via API pública — buscada pelo NAVEGADOR, não por
#    requests direto.
#
#    Por quê: a Armory roda atrás de proteção anti-bot (Cloudflare ou
#    parecido), que costuma liberar navegador de verdade (executa JS,
#    responde desafio) e bloquear clientes HTTP simples — especialmente
#    vindos de IP de datacenter, como o do GitHub Actions. Rodando do
#    seu PC de casa isso quase nunca aparece; rodando na nuvem, é a
#    causa mais comum de 403 Forbidden.
# ------------------------------------------------------------------
def busca_api_via_navegador(pg, nome, realm):
    url = URL_API.format(nome=nome, realm=realm)
    print(f"[api] {url}")

    resp = pg.goto(url, timeout=30000, wait_until="domcontentloaded")

    if resp is None:
        sys.exit("Não consegui abrir a API — sem resposta do servidor.")

    if resp.status == 403:
        sys.exit(
            "A Armory recusou o acesso (403), mesmo pelo navegador.\n"
            "Isso costuma acontecer quando o IP de quem está rodando o\n"
            "script está bloqueado (comum em servidores de nuvem/CI).\n"
            "Tente rodar localmente (atualizar.bat, do seu PC) ou de novo\n"
            "daqui a pouco — bloqueios desse tipo às vezes são temporários."
        )
    if resp.status >= 400:
        sys.exit(f"A API respondeu com erro HTTP {resp.status}.")

    texto = pg.inner_text("body").strip()
    try:
        d = json.loads(texto)
    except json.JSONDecodeError:
        sys.exit(
            "A API não devolveu JSON (provavelmente uma página de desafio\n"
            "anti-bot em vez dos dados). Confira o nome do personagem, o\n"
            "realm, e se o site não está pedindo verificação manual agora."
        )

    if "error" in d:
        sys.exit(f"A API devolveu erro: {d['error']}")
    if not d.get("name"):
        sys.exit("Personagem não encontrado na API.")
    return d


# ------------------------------------------------------------------
# 2) Ícone (e, quando dá, qualidade) de cada item — direto da própria
#    Armory do Warmane, não do Wowhead nem do cavernoftime.
#
#    Por quê: um servidor privado pode ter itens custom, com dados
#    diferentes do banco do Wowhead "de verdade". A fonte mais
#    confiável é a própria página que a Armory já mostra — e ela já
#    está aberta no mesmo navegador que captura o modelo, então isso
#    não custa nenhuma requisição extra a mais.
#
#    Como funciona: a página de perfil desenha os 19 slots de
#    equipamento na mesma ordem do array SLOTS (cabeça, pescoço,
#    ombro... até a arma à distância). Cada slot com item tem um
#    <img src=".../icons/large/xxx.jpg">; slot vazio não tem <img>
#    nenhum. A gente lê essas imagens na ordem em que aparecem na
#    página e casa, uma por uma, com os slots que a API disse ter
#    item — sem precisar saber o nome de nenhuma classe CSS.
# ------------------------------------------------------------------
# Palavras que aparecem na tooltip indicando o slot do item, na ordem
# dos 19 espaços da ficha de personagem.
SLOT_POR_TEXTO = [
    (0,  ["head"]),
    (1,  ["neck"]),
    (2,  ["shoulder"]),
    (3,  ["back"]),
    (4,  ["chest", "robe"]),
    (5,  ["shirt"]),
    (6,  ["tabard"]),
    (7,  ["wrist"]),
    (8,  ["hands"]),
    (9,  ["waist"]),
    (10, ["legs"]),
    (11, ["feet"]),
    (12, ["finger"]),
    (14, ["trinket"]),
    (16, ["two-hand", "main hand", "one-hand"]),
    (17, ["off hand", "held in off-hand", "off-hand", "shield"]),
    (18, ["ranged", "relic", "thrown", "wand", "bow", "gun", "crossbow",
          "idol", "libram", "totem", "sigil"]),
]


def slot_pela_tooltip(texto, ocupados):
    """Descobre em qual dos 19 espaços o item entra, lendo a tooltip.

    Isso resolve um problema real: a API do Warmane devolve só os itens
    equipados, em sequência, sem dizer a que slot cada um pertence. Sem
    isso, um personagem com espaços vazios no meio fica com tudo
    deslocado (a varinha aparecendo na cintura, por exemplo).
    """
    if not texto:
        return None
    baixo = texto.lower()

    for base, palavras in SLOT_POR_TEXTO:
        if not any(pal in baixo for pal in palavras):
            continue
        # Anel e berloque têm dois espaços cada: usa o primeiro livre.
        if base in (12, 14):
            for cand in (base, base + 1):
                if cand not in ocupados:
                    return cand
            return None
        if base not in ocupados:
            return base
    return None


def captura_tooltips(pg, imgs_info):
    """Passa o mouse em cada ícone da página e lê a tooltip que aparece.

    Ler a tooltip da própria Armory é mais confiável do que consultar
    um banco externo: o servidor é privado e pode ter itens que não
    existem no banco do jogo oficial.
    """
    print("[web] lendo tooltips dos itens...")
    lidas = 0

    for info in imgs_info:
        info["tooltip"] = None
        seletor = info.get("seletor")
        if not seletor:
            continue
        try:
            pg.hover(seletor, timeout=3000)
            pg.wait_for_timeout(320)
            texto = pg.evaluate("""() => {
                // A tooltip é o elemento flutuante visível que apareceu
                // por cima da página. Procuramos o menor candidato, que
                // é o mais específico (evita pegar um container inteiro).
                const cands = [...document.querySelectorAll('div, table')]
                  .filter(e => {
                    const cs = getComputedStyle(e);
                    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                    if (cs.position !== 'absolute' && cs.position !== 'fixed') return false;
                    const r = e.getBoundingClientRect();
                    if (r.width < 60 || r.height < 30) return false;
                    if (r.width > 600 || r.height > 700) return false;
                    const t = (e.innerText || '').trim();
                    return t.length > 10 && t.length < 1200;
                  });
                if (!cands.length) return null;
                cands.sort((a, b) => {
                  const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
                  return (ra.width * ra.height) - (rb.width * rb.height);
                });
                return cands[0].innerText.trim();
            }""")
            if texto:
                info["tooltip"] = texto
                lidas += 1
        except Exception:
            pass

    # Tira o mouse de cima pra tooltip não sobrar no screenshot do modelo.
    try:
        pg.mouse.move(5, 5)
        pg.wait_for_timeout(200)
    except Exception:
        pass

    print(f"[web] tooltips lidas: {lidas}/{len(imgs_info)}")
    return lidas


def infere_slots_por_posicao(infos):
    """Descobre o slot de cada ícone pela POSIÇÃO dele na página.

    A ficha da Armory desenha os espaços em posições fixas — duas
    colunas laterais e uma fileira embaixo, igual ao jogo. Então a
    coordenada do ícone diz o slot direto, sem depender de texto.

    Isso é mais robusto que ler a tooltip: funciona mesmo se a Armory
    mudar o texto, traduzir, ou não mostrar tooltip nenhuma.
    """
    pos = [i for i in infos if i.get("x") is not None]
    if len(pos) < 2:
        return 0

    largura_icone = next((i.get("w") for i in pos if i.get("w")), 40) or 40
    xs = sorted(i["x"] for i in pos)
    x_min, x_max = xs[0], xs[-1]
    espalhamento = x_max - x_min

    # Só faz sentido falar em "duas colunas" se os ícones estiverem
    # realmente afastados na horizontal. Sem essa checagem, um
    # personagem que só tem as armas equipadas viraria três colunas
    # de um item cada.
    if espalhamento < largura_icone * 4:
        altura_icone = next((i.get("h") for i in pos if i.get("h")), 40) or 40
        alcance_y = max(i["y"] for i in pos) - min(i["y"] for i in pos)
        if alcance_y > altura_icone * 2:
            esquerda, direita, meio = list(pos), [], []   # uma coluna só
        else:
            esquerda, direita, meio = [], [], list(pos)   # uma fileira só
    else:
        tol = max(20.0, espalhamento * 0.12)
        esquerda = [i for i in pos if abs(i["x"] - x_min) <= tol]
        direita  = [i for i in pos if abs(i["x"] - x_max) <= tol and i not in esquerda]
        meio     = [i for i in pos if i not in esquerda and i not in direita]

    def mdc_aprox(difs, minimo):
        """Maior valor que divide (aproximadamente) todas as distâncias."""
        difs = [d for d in difs if d > 1]
        if not difs:
            return None
        candidatos = []
        for d in difs:
            for k in range(1, 9):
                c = d / k
                if c >= minimo:
                    candidatos.append(c)
        candidatos.sort(reverse=True)
        for c in candidatos:
            if all(abs(d / c - round(d / c)) < 0.18 for d in difs):
                return c
        return min(difs)

    # O espaçamento é o mesmo nas duas colunas, então junta as
    # distâncias de ambas antes de estimar. Isso importa: uma coluna
    # sozinha com poucos itens pode ter todas as distâncias em dobro,
    # e o espaçamento sairia multiplicado.
    altura = next((i.get("h") for i in pos if i.get("h")), 1) or 1
    difs_globais = []
    for grupo in (esquerda, direita):
        ys = sorted(i["y"] for i in grupo)
        difs_globais += [b - a for a, b in zip(ys, ys[1:])]
    passo = mdc_aprox(difs_globais, max(altura * 0.8, 8)) or altura

    # As duas colunas começam na mesma altura na ficha, então o topo é
    # o ícone mais alto entre as duas — assim o índice continua certo
    # mesmo se o primeiro espaço de uma delas estiver vazio.
    topo_ref = min((i["y"] for i in esquerda + direita), default=None)

    def numera(grupo, base, quantos):
        if not grupo:
            return
        grupo.sort(key=lambda i: i["y"])
        origem = topo_ref if topo_ref is not None else grupo[0]["y"]
        for it in grupo:
            idx = int(round((it["y"] - origem) / passo))
            slot = base + max(0, min(idx, quantos - 1))
            it["slot_geo"] = slot

    numera(esquerda, 0, 8)
    numera(direita, 8, 8)
    if meio:
        meio.sort(key=lambda i: i["x"])
        largura = next((i.get("w") for i in meio if i.get("w")), 1) or 1
        difs_x = [b["x"] - a["x"] for a, b in zip(meio, meio[1:])]
        passo_x = mdc_aprox(difs_x, max(largura * 0.8, 8)) or largura
        origem_x = meio[0]["x"]
        for it in meio:
            idx = int(round((it["x"] - origem_x) / passo_x))
            it["slot_geo"] = 16 + max(0, min(idx, 2))

    achou = sum(1 for i in pos if i.get("slot_geo") is not None)
    print(f"[web] geometria: {len(esquerda)} à esquerda, {len(direita)} à direita, "
          f"{len(meio)} embaixo -> {achou} slot(s) posicionado(s)")
    return achou


def escreve_diagnostico(infos, pasta):
    """Guarda o que foi lido da página, pra facilitar ajuste fino se o
    posicionamento sair errado."""
    dados = []
    for i, info in enumerate(infos):
        dados.append({
            "ordem": i,
            "src": info.get("src"),
            "x": info.get("x"), "y": info.get("y"),
            "w": info.get("w"), "h": info.get("h"),
            "slot_geo": info.get("slot_geo"),
            "tooltip": (info.get("tooltip") or "")[:400],
            "html": (info.get("html") or "")[:400],
        })
    destino = pasta / "diagnostico.json"
    destino.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[web] diagnóstico salvo em {destino}")


def extrai_icones_do_perfil(pg, equipamento_api, pasta):
    """Preenche 'icone_url', 'tooltip' e 'slot_real' em cada item de
    equipamento_api, direto do HTML já carregado em pg."""
    try:
        infos = pg.eval_on_selector_all(
            'img[src*="/icons/large/"]',
            """els => els.map((e, i) => {
                e.setAttribute('data-cap', 'icone-' + i);
                const a = e.closest('a');
                const r = e.getBoundingClientRect();
                let q = null;
                if (a) {
                    const m = (a.className || '').match(/\\bq([0-7])\\b/);
                    if (m) q = parseInt(m[1]);
                }
                // guarda um pedaço do HTML em volta: se o posicionamento
                // sair errado, é isso que mostra como a página organiza
                // os espaços de equipamento.
                const volta = (a && a.parentElement) ? a.parentElement : e.parentElement;
                return {
                    src: e.getAttribute('src'),
                    qualidade: q,
                    x: Math.round(r.x), y: Math.round(r.y),
                    w: Math.round(r.width), h: Math.round(r.height),
                    html: volta ? volta.outerHTML.slice(0, 400) : null,
                    seletor: '[data-cap="icone-' + i + '"]'
                };
            })"""
        )
    except Exception as e:
        print(f"[aviso] não consegui ler os ícones da página de perfil: {e}")
        infos = []

    if not infos:
        return

    infere_slots_por_posicao(infos)
    captura_tooltips(pg, infos)

    fila = list(infos)
    achados = 0
    ocupados = set()

    for item in equipamento_api:
        if not item.get("name") or not fila:
            continue
        info = fila.pop(0)
        item["icone_url"] = info["src"]
        item["tooltip"] = info.get("tooltip")
        if info["qualidade"] is not None:
            item["qualidade"] = info["qualidade"]

        # A tooltip vem primeiro: quando ela existe, diz o slot sem
        # ambiguidade ("Ranged", "Off Hand"...). A geometria entra
        # quando a tooltip não foi capturada — ela acerta as colunas,
        # mas pode se confundir na fileira de armas se faltar peça.
        slot = slot_pela_tooltip(info.get("tooltip"), ocupados)
        if slot is None:
            geo = info.get("slot_geo")
            if geo is not None and geo not in ocupados:
                slot = geo
        if slot is not None and slot not in ocupados:
            ocupados.add(slot)
            item["slot_real"] = slot
        achados += 1

    escreve_diagnostico(infos, pasta)

    equipados = sum(1 for it in equipamento_api if it.get("name"))
    com_slot = sum(1 for it in equipamento_api if it.get("slot_real") is not None)
    print(f"[web] ícones casados: {achados}/{equipados} | slot definido: {com_slot}/{equipados}")


def baixa_icone_url(url, pasta):
    """Baixa um ícone a partir da URL completa (já vem pronta da Armory,
    não precisa montar nome de arquivo nem base de CDN)."""
    if not url:
        return None
    nome_arquivo = url.rstrip("/").split("/")[-1].split("?")[0]
    if not nome_arquivo:
        return None
    destino = pasta / nome_arquivo
    if destino.exists():
        return destino.name
    try:
        r = requests.get(url, headers=UA, timeout=15)
        r.raise_for_status()
        destino.write_bytes(r.content)
        return destino.name
    except Exception as e:
        print(f"  [aviso] falhou baixar ícone {url}: {e}")
        return None


# ------------------------------------------------------------------
# 3) Atributos + modelo 3D, via navegador
# ------------------------------------------------------------------
def _num(txt):
    m = re.search(r"([\d.,]+%?)", txt or "")
    return m.group(1) if m else None


def extrai_stats(texto):
    """Recorta o texto da página em seções e pega os valores que
    interessam pra um caster. A Armory usa rótulos repetidos entre
    seções (Power, Critical, Hit rating), por isso a leitura é feita
    dentro de cada seção, e não no texto inteiro."""
    secoes = {}
    marcadores = ["Melee", "Attributes", "Ranged", "Defense", "Spell", "Resistances"]
    pos = []
    for m in marcadores:
        i = texto.find(m)
        if i >= 0:
            pos.append((i, m))
    pos.sort()
    for k, (i, nome) in enumerate(pos):
        fim = pos[k + 1][0] if k + 1 < len(pos) else len(texto)
        secoes[nome] = texto[i:fim]

    def campo(secao, rotulo):
        bloco = secoes.get(secao, "")
        m = re.search(rotulo + r"\s*:\s*([\d.,]+%?)", bloco)
        return m.group(1) if m else None

    return {
        "spellPower": campo("Spell", "Power"),
        "spellHit":   campo("Spell", "Hit rating"),
        "spellCrit":  campo("Spell", "Critical"),
        "haste":      campo("Spell", "Haste"),
        "intellect":  campo("Attributes", "Intellect"),
        "stamina":    campo("Attributes", "Stamina"),
        "spirit":     campo("Attributes", "Spirit"),
        "armor":      campo("Defense", "Armor"),
    }


def captura_tudo_via_navegador(nome, realm, pasta, frames, debug, giro_manual=None, tolerancia_fundo=TOLERANCIA_FUNDO):
    """Abre o Chromium uma única vez e faz nele as três coisas que
    dependem da Armory: buscar a API, ler os atributos, e capturar o
    modelo. Um navegador só, reaproveitado — mais rápido e evita abrir
    e fechar Chromium várias vezes à toa."""
    stats, arquivos = {}, []

    with sync_playwright() as p:
        nav = p.chromium.launch(headless=not debug)
        pg = nav.new_page(
            viewport={"width": 1400, "height": 950},
            user_agent=UA["User-Agent"],
        )

        # --- 1) API (equipamento, guilda, nível, etc.) ---
        api = busca_api_via_navegador(pg, nome, realm)

        # --- 2) perfil: atributos + modelo 3D ---
        url = URL_PERFIL.format(nome=nome, realm=realm)
        print(f"[web] {url}")
        pg.goto(url, timeout=45000, wait_until="networkidle")

        try:
            texto = pg.inner_text("body")
            stats = extrai_stats(texto)
            achou = sum(1 for v in stats.values() if v)
            print(f"[web] atributos lidos: {achou}/8")
        except Exception as e:
            print(f"[aviso] não consegui ler os atributos: {e}")

        # --- 3) ícones do equipamento, direto da mesma página ---
        extrai_icones_do_perfil(pg, api.get("equipment", []), pasta)

        print(f"[web] esperando {ESPERA_MODELO_S}s o modelo renderizar...")
        pg.wait_for_timeout(ESPERA_MODELO_S * 1000)

        canvas = maior_canvas(pg)
        if not canvas:
            print("[aviso] não achei o canvas do modelo — o painel vai ficar sem imagem.")
        else:
            arquivos = gira_e_captura(pg, canvas, pasta, frames, giro_manual, tolerancia_fundo)

        nav.close()

    return api, stats, arquivos


def maior_canvas(pg):
    """O visualizador é, com folga, o maior <canvas> da página."""
    melhor, area_melhor = None, 0
    for c in pg.query_selector_all("canvas"):
        box = c.bounding_box()
        if not box:
            continue
        area = box["width"] * box["height"]
        if area > area_melhor:
            area_melhor, melhor = area, c
    if area_melhor < 10000:
        return None
    return melhor


# ------------------------------------------------------------------
# Remoção de fundo dos frames do modelo
#
# Por quê: o navegador tira o screenshot de um <canvas> WebGL, e esse
# canvas limpa com uma cor sólida (alpha=1) antes de desenhar — quase
# todo visualizador 3D faz isso. Resultado: o PNG capturado NUNCA é
# transparente, mesmo que pareça "vazio" ao redor do personagem. Sem
# tratar isso, o CSS do painel desenha uma sombra (drop-shadow) em
# volta desse retângulo sólido — é o "quadro com sombra" que aparece
# ao redor do modelo.
#
# Como resolve: acha a cor do fundo pelos 4 cantos da imagem (onde tem
# certeza que não há personagem) e apaga, por flood-fill, só os
# pixels CONECTADOS a esses cantos com cor parecida. Isso preserva
# partes escuras do personagem (armadura preta, cabelo escuro) que
# não tocam a borda da imagem — só o que está grudado no fundo some.
# ------------------------------------------------------------------
# ------------------------------------------------------------------
# Remoção de fundo dos frames do modelo
# ------------------------------------------------------------------
def remove_fundo(caminho_png, tolerancia=TOLERANCIA_FUNDO):
    """Torna transparente o fundo sólido de um frame capturado do canvas.
    Sobrescreve o próprio arquivo. Reescrevido para usar Pillow puro."""
    from PIL import Image, ImageDraw

    im = Image.open(caminho_png).convert("RGB")
    w, h = im.size

    # Pega as cores dos 4 cantos para estimar o fundo
    cantos = [
        im.getpixel((0, 0)),
        im.getpixel((w - 1, 0)),
        im.getpixel((0, h - 1)),
        im.getpixel((w - 1, h - 1))
    ]
    # bg = cor mais frequente nos cantos
    bg = max(set(cantos), key=cantos.count)

    trabalho = im.copy()
    
    # Uma cor-sentinela berrante e improvável de existir no modelo real
    MARCA = (255, 0, 255)

    # Preenche o fundo com a cor MARCA a partir dos 4 cantos
    for x, y in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        if trabalho.getpixel((x, y)) != MARCA:
            ImageDraw.floodfill(trabalho, (x, y), MARCA, thresh=tolerancia)

    # Extrai os pixels
    dados_rgb = im.getdata()
    dados_trab = trabalho.getdata()
    novos_dados = []

    removidos = 0
    for pixel_orig, pixel_trab in zip(dados_rgb, dados_trab):
        if pixel_trab == MARCA:
            # Fundo: converte para alpha 0
            novos_dados.append((pixel_orig[0], pixel_orig[1], pixel_orig[2], 0))
            removidos += 1
        else:
            # Personagem: mantém opaco (alpha 255)
            novos_dados.append((pixel_orig[0], pixel_orig[1], pixel_orig[2], 255))

    # Cria imagem com transparência (RGBA)
    im_rgba = Image.new("RGBA", im.size)
    im_rgba.putdata(novos_dados)
    im_rgba.save(caminho_png)

    return bg, removidos, w * h


def _miniatura(img_bytes):
    """Reduz o screenshot a uma miniatura em tons de cinza, pra comparar
    frames de forma barata."""
    from PIL import Image
    import io
    return Image.open(io.BytesIO(img_bytes)).convert("L").resize((80, 80))


def _diferenca(a, b):
    """Diferença média entre duas miniaturas. 0 = idênticas."""
    from PIL import ImageChops
    dados = ImageChops.difference(a, b).getdata()
    return sum(dados) / len(dados)


def _arrasta(pg, cx, cy, distancia, assenta_ms=240):
    pg.mouse.move(cx, cy)
    pg.mouse.down()
    pg.mouse.move(cx + distancia, cy, steps=6)
    pg.mouse.up()
    pg.wait_for_timeout(assenta_ms)


def calibra_volta_completa(pg, canvas, cx, cy, largura):
    """Descobre quanto é preciso arrastar o mouse pra o personagem dar
    uma volta inteira.

    Por que isso existe: a sensibilidade do visualizador da Armory é
    desconhecida — não dá pra saber de fora quantos pixels de arrasto
    equivalem a 360°. Chutar esse valor faz o último frame não encostar
    no primeiro, e a animação "pula" ao reiniciar o loop.

    Como funciona: tira uma foto de referência, vai girando de pouquinho
    e comparando cada foto com a referência. A diferença sobe conforme o
    personagem vira de costas e volta a cair conforme ele completa a
    volta. O ponto de menor diferença DEPOIS do pico é a volta completa.
    """
    PASSO = largura * 0.04
    MAX_SONDAS = 70                      # cobre até 2,8× a largura do canvas

    try:
        ref = _miniatura(canvas.screenshot())
    except Exception as e:
        print(f"  [aviso] calibração não pôde começar: {e}")
        return None

    diffs = []
    for i in range(MAX_SONDAS):
        _arrasta(pg, cx, cy, PASSO, assenta_ms=170)
        try:
            diffs.append(_diferenca(ref, _miniatura(canvas.screenshot())))
        except Exception:
            break

    if len(diffs) < 8:
        print("  [aviso] calibração juntou poucas amostras.")
        return None

    pico = max(diffs)
    if pico < 2.0:
        print("  [aviso] o modelo não girou com o arrasto — nada mudou na tela.")
        return None

    # Por que limiar e não "primeiro mínimo local": de costas o personagem
    # ainda lembra a pose inicial, então 180° também é um mínimo local e
    # o algoritmo pararia lá, cortando a volta pela metade. Voltando de
    # verdade ao ponto de partida a diferença despenca pra perto de zero —
    # ordem de grandeza abaixo do vale dos 180°. Daí o corte por limiar.
    LIMIAR = 0.32
    alvo = pico * LIMIAR

    # Sem suavizar: quando há poucas amostras por volta, o vale do
    # retorno tem 1 ponto só, e média móvel o borraria a ponto de sumir.
    afastou = False
    i_volta = None
    for i, v in enumerate(diffs):
        if v > pico * 0.5:
            afastou = True          # já virou o bastante pra valer
        elif afastou and v < alvo:
            i_volta = i
            break

    if i_volta is None:
        print("  [aviso] o giro não voltou à pose inicial dentro do alcance sondado.")
        return None

    total = (i_volta + 1) * PASSO
    print(f"[web] volta completa ≈ {total:.0f}px de arrasto "
          f"({(total/largura):.2f}× a largura do modelo)")
    return total


def gira_e_captura(pg, canvas, pasta, frames, giro_manual=None, tolerancia_fundo=TOLERANCIA_FUNDO):
    """Gira o personagem e fotografa em N poses igualmente espaçadas,
    fechando o loop direitinho (último frame encosta no primeiro)."""
    box = canvas.bounding_box()
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    largura = box["width"]

    if giro_manual:
        volta = largura * giro_manual
        print(f"[web] usando giro manual: {giro_manual:.2f}× a largura")
    else:
        print("[web] calibrando quanto é uma volta completa...")
        volta = calibra_volta_completa(pg, canvas, cx, cy, largura)
        if volta is None:
            volta = largura * 0.75
            print("  [aviso] caindo no valor padrão (0.75×). Se a animação "
                  "ficar com um salto, ajuste com --giro (ex: --giro 1.2).")

    # Dividir a volta por 'frames' (não por 'frames-1') é o que fecha o
    # loop: o frame seguinte ao último coincidiria com o primeiro.
    passo = volta / max(frames, 1)

    arquivos = []
    print(f"[web] capturando {frames} frame(s)...")

    # Limpa frames de execuções anteriores. Sem isso, trocar pra um
    # personagem com menos frames deixaria arquivos órfãos na pasta.
    for antigo in pasta.glob("modelo_*.png"):
        try:
            antigo.unlink()
        except Exception:
            pass

    for i in range(frames):
        nome_arq = f"modelo_{i:02d}.png"
        try:
            canvas.screenshot(path=str(pasta / nome_arq))
            if tolerancia_fundo:
                try:
                    remove_fundo(pasta / nome_arq, tolerancia_fundo)
                except Exception as e:
                    print(f"  [aviso] não consegui remover o fundo do frame {i}: {e}")
            arquivos.append(nome_arq)
        except Exception as e:
            print(f"  [aviso] frame {i} falhou: {e}")
            break

        if i < frames - 1:
            _arrasta(pg, cx, cy, passo)

    return arquivos


# ------------------------------------------------------------------
# Montagem final
# ------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Gera os dados da Twitch Extension do Warmane.")
    ap.add_argument("--nome",   default=NOME_PADRAO)
    ap.add_argument("--realm",  default=REALM_PADRAO)
    ap.add_argument("--frames", type=int, default=FRAMES_PADRAO,
                    help="quantos frames de rotação capturar (1 = imagem parada)")
    ap.add_argument("--saida",  default=SAIDA_PADRAO)
    ap.add_argument("--giro",   type=float, default=None,
                    help="pula a calibração e usa este multiplicador da largura "
                         "do modelo como uma volta completa (ex: 1.2)")
    ap.add_argument("--debug",  action="store_true", help="abre o navegador visível")
    ap.add_argument("--tolerancia-fundo", type=int, default=TOLERANCIA_FUNDO,
                    help=f"quão parecido com o fundo um pixel precisa ser pra "
                         f"virar transparente (padrão {TOLERANCIA_FUNDO}). Suba se "
                         f"sobrar fundo visível nas bordas do modelo; desça se "
                         f"partes escuras do personagem estiverem sumindo")
    ap.add_argument("--sem-remover-fundo", action="store_true",
                    help="desliga a remoção de fundo, deixa o frame como veio do canvas")
    args = ap.parse_args()

    pasta = Path(args.saida)
    pasta_icones = pasta / "icones"
    pasta.mkdir(parents=True, exist_ok=True)
    pasta_icones.mkdir(parents=True, exist_ok=True)

    tolerancia_fundo = 0 if args.sem_remover_fundo else args.tolerancia_fundo
    api, stats, frames = captura_tudo_via_navegador(
        args.nome, args.realm, pasta, args.frames, args.debug, args.giro, tolerancia_fundo
    )

    print("[itens] baixando ícones da Armory...")

    # Primeiro os que sabem o próprio slot; depois os indefinidos vão
    # preenchendo os espaços que sobraram, em ordem.
    equipamento = [{"slot": i, "nome": None} for i in range(19)]
    sem_slot = []

    for item in api.get("equipment", []):
        nome_item = item.get("name")
        if not nome_item:
            continue
        registro = {
            "nome": nome_item,
            "itemId": item.get("item"),
            "icone_url": item.get("icone_url"),
            "qualidade": item.get("qualidade"),
            "tooltip": item.get("tooltip"),
        }
        slot = item.get("slot_real")
        if slot is not None and 0 <= slot < 19 and equipamento[slot]["nome"] is None:
            registro["slot"] = slot
            equipamento[slot] = registro
        else:
            sem_slot.append(registro)

    for registro in sem_slot:
        for i in range(19):
            if equipamento[i]["nome"] is None:
                registro["slot"] = i
                equipamento[i] = registro
                break

    for registro in equipamento:
        if registro.get("nome") is None:
            continue
        arquivo_icone = baixa_icone_url(registro.pop("icone_url", None), pasta_icones)
        registro["icone"] = f"icones/{arquivo_icone}" if arquivo_icone else ""

    dados = {
        "nome":   api.get("name"),
        "realm":  api.get("realm", args.realm),
        "guilda": api.get("guild") or "",
        "nivel":  api.get("level"),
        "raca":   api.get("race"),
        "classe": api.get("class"),
        "faccao": api.get("faction", ""),
        "online": bool(api.get("online")),
        "pontosConquista": api.get("achievementpoints"),
        "abates": api.get("honorablekills"),
        "modeloFrames": frames,
        "equipamento": equipamento,
        "stats": stats,
        # Muda a cada execução. O painel pendura isso na URL das imagens
        # do modelo pra furar o cache: os arquivos têm sempre o mesmo
        # nome (modelo_00.png...), então sem isso o navegador continuaria
        # mostrando o modelo do personagem anterior.
        "versao": str(int(time.time())),
        "atualizadoEm": time.strftime("%d/%m %H:%M"),
    }

    destino = pasta / "dados.json"
    destino.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"OK — {destino}")
    print(f"     {len(frames)} frame(s) de modelo")
    print(f"     {sum(1 for e in equipamento if e.get('nome'))} item(ns) equipado(s)")
    print(f"     {sum(1 for v in stats.values() if v)}/8 atributos")
    print()
    print("Agora publique a pasta inteira e aponte BASE_DADOS no panel.html pra ela.")


if __name__ == "__main__":
    main()
