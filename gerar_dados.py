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
  5. Resolve ícone e qualidade de cada item no Wowhead
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
    pip install playwright requests
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
URL_TOOLTIP = "https://nether.wowhead.com/tooltip/item/{id}?dataEnv=4&locale=0"
URL_ICONE   = "https://wow.zamimg.com/images/wow/icons/large/{icone}.jpg"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

ESPERA_MODELO_S = 9         # tempo pro WebGL carregar o modelo


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
# 2) Ícone e qualidade de cada item, via Wowhead
# ------------------------------------------------------------------
_cache_item = {}

def info_item(item_id):
    if item_id in _cache_item:
        return _cache_item[item_id]
    try:
        r = requests.get(URL_TOOLTIP.format(id=item_id), headers=UA, timeout=15)
        r.raise_for_status()
        j = r.json()
        info = {"icone": j.get("icon"), "qualidade": j.get("quality")}
    except Exception as e:
        print(f"  [aviso] não consegui dados do item {item_id}: {e}")
        info = {"icone": None, "qualidade": None}
    _cache_item[item_id] = info
    time.sleep(0.12)          # educação com o servidor deles
    return info


def baixa_icone(nome_icone, pasta):
    destino = pasta / f"{nome_icone}.jpg"
    if destino.exists():
        return destino.name
    try:
        r = requests.get(URL_ICONE.format(icone=nome_icone), headers=UA, timeout=15)
        r.raise_for_status()
        destino.write_bytes(r.content)
        return destino.name
    except Exception as e:
        print(f"  [aviso] falhou baixar ícone {nome_icone}: {e}")
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


def captura_tudo_via_navegador(nome, realm, pasta, frames, debug):
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

        print(f"[web] esperando {ESPERA_MODELO_S}s o modelo renderizar...")
        pg.wait_for_timeout(ESPERA_MODELO_S * 1000)

        canvas = maior_canvas(pg)
        if not canvas:
            print("[aviso] não achei o canvas do modelo — o painel vai ficar sem imagem.")
        else:
            arquivos = gira_e_captura(pg, canvas, pasta, frames)

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


def gira_e_captura(pg, canvas, pasta, frames):
    """Arrasta o mouse sobre o canvas pra girar o personagem e tira
    uma foto a cada passo. Um giro completo dividido em N frames."""
    box = canvas.bounding_box()
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    passo = box["width"] * 0.75 / max(frames, 1)   # quanto arrastar por frame

    arquivos = []
    print(f"[web] capturando {frames} frame(s)...")

    for i in range(frames):
        nome_arq = f"modelo_{i:02d}.png"
        try:
            canvas.screenshot(path=str(pasta / nome_arq))
            arquivos.append(nome_arq)
        except Exception as e:
            print(f"  [aviso] frame {i} falhou: {e}")
            break

        if i < frames - 1:
            pg.mouse.move(cx, cy)
            pg.mouse.down()
            pg.mouse.move(cx + passo, cy, steps=6)
            pg.mouse.up()
            pg.wait_for_timeout(260)   # deixa o render assentar

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
    ap.add_argument("--debug",  action="store_true", help="abre o navegador visível")
    args = ap.parse_args()

    pasta = Path(args.saida)
    pasta_icones = pasta / "icones"
    pasta.mkdir(parents=True, exist_ok=True)
    pasta_icones.mkdir(parents=True, exist_ok=True)

    api, stats, frames = captura_tudo_via_navegador(
        args.nome, args.realm, pasta, args.frames, args.debug
    )

    print("[itens] resolvendo ícones no Wowhead...")
    equipamento = []
    for idx, item in enumerate(api.get("equipment", [])):
        nome_item = item.get("name")
        if not nome_item:
            equipamento.append({"slot": idx, "nome": None})
            continue
        info = info_item(item.get("item"))
        arquivo_icone = baixa_icone(info["icone"], pasta_icones) if info["icone"] else None
        equipamento.append({
            "slot": idx,
            "nome": nome_item,
            "itemId": item.get("item"),
            "icone": f"icones/{arquivo_icone}" if arquivo_icone else "",
            "qualidade": info["qualidade"],
        })

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
