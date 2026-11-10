"""
BTCTurk İki Dönüş Gölge Motoru

1) ⚡ SAAT İÇİ DÖNÜŞ: 15-60 dakikalık sert çekilme sonrası doğrulanmış dönüş.
2) 🔄 DERİN DÖNÜŞ: birkaç saatlik düşüş sonrası hacimli ve doğrulanmış dönüş.

Bu dosya yalnız gözlem/Telegram mesajı ve başarı ölçümü yapar.
Özel API, bakiye, gerçek AL/SAT veya otomatik emir içermez.
"""

import json
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
_CHAT_ENV = os.getenv("CHAT_IDS", "").strip()
CHAT_IDS = (
    [int(x.strip()) for x in _CHAT_ENV.split(",") if x.strip()]
    if _CHAT_ENV else [2097448038]
)

TARAMA_SURESI = 60
TAKIP_EDILECEK_LIKIT_COIN = 25
MIN_TAHMINI_CIRO_TRY = 1_000_000
MAX_SPREAD = 0.60

# Saat içi dönüş: yakın tepeden çekilme ve dipten toparlanma.
SAATLIK_CEKILIS_MIN = 1.50
SAATLIK_CEKILIS_MAX = 5.00
SAATLIK_DONUS_MIN = 0.50
SAATLIK_MUM_GOVDE_MIN = 0.08
SAATLIK_HACIM_MIN = 1.25

# Derin dönüş: 2-12 saatlik zayıflama ve daha güçlü toparlanma.
DERIN_CEKILIS_MIN = 4.00
DERIN_CEKILIS_MAX = 15.00
DERIN_DONUS_MIN = 1.00
DERIN_HACIM_MIN = 1.50
DERIN_KIRMIZI_PAY_MIN = 0.55

# Genel piyasa güvenlik kapısı. Kötü piyasa tamamen yasak değildir; fakat
# BTC/piyasa yeni bir sert aşağı dalga içindeyken dönüş mesajı verilmez.
BTC_5DK_ALT_SINIR = -1.50
PIYASA_5DK_ALT_SINIR = -1.10

AYNI_TIP_SOGUMA = 3 * 60 * 60
DERIN_SOGUMA = 6 * 60 * 60
SONUC_SURESI = 3 * 60 * 60
YANLIS_ESIK = -1.00
HEDEFLER = (1.0, 1.5, 2.0)

_RAILWAY_VOLUME = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
_DATA_DIR = _RAILWAY_VOLUME if _RAILWAY_VOLUME else ("/data" if os.path.isdir("/data") else ".")
KAYIT_DOSYA = os.getenv(
    "DONUS_GOLGE_DOSYA", os.path.join(_DATA_DIR, "btcturk_iki_donus_golge.json")
)
META_DOSYA = os.getenv(
    "DONUS_GOLGE_META_DOSYA", os.path.join(_DATA_DIR, "btcturk_iki_donus_meta.json")
)

SESSION = requests.Session()


def pct(yeni, eski):
    try:
        return ((float(yeni) / float(eski)) - 1.0) * 100.0 if float(eski) else 0.0
    except Exception:
        return 0.0


def atomik_json_yaz(dosya, veri):
    klasor = os.path.dirname(os.path.abspath(dosya))
    if klasor:
        os.makedirs(klasor, exist_ok=True)
    gecici = dosya + ".tmp"
    with open(gecici, "w", encoding="utf-8") as f:
        json.dump(veri, f, ensure_ascii=False)
    os.replace(gecici, dosya)


def json_oku(dosya, varsayilan):
    try:
        if os.path.exists(dosya):
            with open(dosya, "r", encoding="utf-8") as f:
                veri = json.load(f)
            return veri
    except Exception as e:
        print("Kayıt okunamadı:", dosya, e)
    return varsayilan


KAYITLAR = json_oku(KAYIT_DOSYA, [])
META = json_oku(META_DOSYA, {"son_rapor": time.time()})


def telegram_gonder(mesaj):
    if not BOT_TOKEN:
        print(mesaj)
        print("BOT_TOKEN yok; mesaj yalnız loga yazıldı.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for chat_id in CHAT_IDS:
        try:
            r = SESSION.post(
                url, json={"chat_id": chat_id, "text": mesaj}, timeout=12
            )
            r.raise_for_status()
        except Exception as e:
            print("Telegram hatası:", chat_id, e)


def ticker_getir():
    r = SESSION.get("https://api.btcturk.com/api/v2/ticker", timeout=12)
    r.raise_for_status()
    return r.json().get("data", [])


def kline_getir(symbol, resolution, dakika):
    simdi = int(time.time())
    url = "https://graph-api.btcturk.com/v1/klines/history"
    r = SESSION.get(
        url,
        params={
            "symbol": symbol,
            "resolution": resolution,
            "from": simdi - dakika * 60,
            "to": simdi,
        },
        timeout=12,
    )
    r.raise_for_status()
    d = r.json()
    return d if isinstance(d, dict) else {}


def orderbook_ozeti(symbol):
    try:
        r = SESSION.get(
            "https://api.btcturk.com/api/v2/orderbook",
            params={"pairSymbol": symbol, "limit": 10},
            timeout=8,
        )
        r.raise_for_status()
        d = r.json().get("data", {})
        bids = d.get("bids", [])
        asks = d.get("asks", [])
        if not bids or not asks:
            return {}
        bid = float(bids[0][0])
        ask = float(asks[0][0])
        orta = (bid + ask) / 2.0
        spread = ((ask - bid) / orta) * 100.0 if orta else 99.0
        bid_tutar = sum(float(x[0]) * float(x[1]) for x in bids[:10])
        ask_tutar = sum(float(x[0]) * float(x[1]) for x in asks[:10])
        dengesizlik = bid_tutar / ask_tutar if ask_tutar > 0 else 0.0
        return {"bid": bid, "ask": ask, "spread": spread, "dengesizlik": dengesizlik}
    except Exception:
        return {}


def stable_coin(symbol):
    return symbol in {"USDTTRY", "USDCTRY", "DAITRY"}


def likit_coinleri_sec(ticker):
    secilen = []
    for x in ticker:
        try:
            symbol = str(x.get("pair", ""))
            if not symbol.endswith("TRY") or symbol == "BTCTRY" or stable_coin(symbol):
                continue
            fiyat = float(x.get("last", 0) or 0)
            hacim = float(x.get("volume", 0) or 0)
            ortalama = float(x.get("average", 0) or fiyat)
            ciro = hacim * ortalama
            bid = float(x.get("bid", 0) or 0)
            ask = float(x.get("ask", 0) or 0)
            orta = (bid + ask) / 2 if bid > 0 and ask > 0 else 0
            spread = ((ask - bid) / orta) * 100 if orta else 99
            if fiyat <= 0 or ciro < MIN_TAHMINI_CIRO_TRY or spread > MAX_SPREAD:
                continue
            secilen.append((ciro, symbol, fiyat, spread))
        except Exception:
            continue
    secilen.sort(reverse=True)
    return secilen[:TAKIP_EDILECEK_LIKIT_COIN]


def mumlari_duzelt(d):
    """Son kapanmış 1dk mumunu esas alan ortak görünüm."""
    try:
        o = [float(x) for x in d.get("o", [])]
        h = [float(x) for x in d.get("h", [])]
        l = [float(x) for x in d.get("l", [])]
        c = [float(x) for x in d.get("c", [])]
        v = [float(x) for x in d.get("v", [])]
        t = d.get("t", list(range(len(c))))
        n = min(len(o), len(h), len(l), len(c), len(v), len(t))
        if n < 15:
            return {}
        # Son eleman oluşmakta olan mum olabilir; sinyalde yalnız kapanmış -2 kullanılır.
        return {"o": o[:n], "h": h[:n], "l": l[:n], "c": c[:n], "v": v[:n], "t": t[:n]}
    except Exception:
        return {}


def coin_verisini_getir(symbol):
    try:
        bir = mumlari_duzelt(kline_getir(symbol, 1, 100))
        onbes = mumlari_duzelt(kline_getir(symbol, 15, 12 * 60))
        return symbol, bir, onbes
    except Exception as e:
        print(symbol, "kline hatası:", e)
        return symbol, {}, {}


def temel_1dk_ozet(d):
    if not d or len(d["c"]) < 65:
        return {}
    o, h, l, c, v, t = d["o"], d["h"], d["l"], d["c"], d["v"], d["t"]
    i = -2
    onceki_hacimler = v[-22:-2]
    med_hacim = statistics.median(onceki_hacimler) if onceki_hacimler else 0
    hacim_x = v[i] / med_hacim if med_hacim > 0 else 0
    govde = pct(c[i], o[i])
    d3 = pct(c[i], c[-5])
    d5 = pct(c[i], c[-7])
    tepe60 = max(h[-62:-2])
    dip15 = min(l[-17:-2])
    cekilis = pct(dip15, tepe60)
    donus = pct(c[i], dip15)
    yesil = c[i] > o[i] and c[i] > c[i - 1]
    onceki_teyit = c[i] >= h[i - 1] or (govde >= 0.18 and d3 > 0)
    return {
        "kapanis": c[i], "mum_zamani": t[i], "hacim_x": hacim_x,
        "govde": govde, "d3": d3, "d5": d5, "tepe60": tepe60,
        "dip15": dip15, "cekilis": cekilis, "donus": donus,
        "yesil": yesil, "teyit": onceki_teyit,
    }


def derin_ozet(d15, bir_ozet):
    if not d15 or len(d15["c"]) < 34 or not bir_ozet:
        return {}
    o, h, l, c = d15["o"], d15["h"], d15["l"], d15["c"]
    # Son 8 saatin tepesi ile son iki saatin dibi arasındaki ana düşüş.
    tepe8s = max(h[-34:-2])
    dip2s = min(l[-10:-1])
    cekilis = pct(dip2s, tepe8s)
    donus = pct(bir_ozet["kapanis"], dip2s)
    son8 = list(zip(o[-10:-2], c[-10:-2]))
    kirmizi_pay = sum(1 for ac, kap in son8 if kap < ac) / len(son8) if son8 else 0
    saat3 = pct(c[-2], c[-14]) if len(c) >= 14 else 0.0
    return {
        "tepe8s": tepe8s, "dip2s": dip2s, "cekilis": cekilis,
        "donus": donus, "kirmizi_pay": kirmizi_pay, "saat3": saat3,
    }


def son_mesaj_zamani(symbol, tip):
    for k in reversed(KAYITLAR):
        if k.get("symbol") == symbol and k.get("tip") == tip:
            return float(k.get("zaman", 0) or 0)
    return 0.0


def kayit_ac(symbol, tip, fiyat, oz, derin, ob, btc5, piyasa5):
    event = {
        "symbol": symbol, "tip": tip, "zaman": time.time(), "giris": fiyat,
        "mum_zamani": oz.get("mum_zamani"), "max_getiri": 0.0,
        "min_getiri": 0.0, "son_getiri": 0.0, "tamamlandi": False,
        "cekilis": derin.get("cekilis") if tip == "derin" else oz.get("cekilis"),
        "donus": derin.get("donus") if tip == "derin" else oz.get("donus"),
        "hacim_x": oz.get("hacim_x"), "d3": oz.get("d3"), "d5": oz.get("d5"),
        "spread": ob.get("spread"), "orderbook": ob.get("dengesizlik"),
        "btc5": btc5, "piyasa5": piyasa5,
    }
    KAYITLAR.append(event)
    del KAYITLAR[:-4000]
    atomik_json_yaz(KAYIT_DOSYA, KAYITLAR)
    return event


def mesaj_uret(event):
    coin = str(event["symbol"]).replace("TRY", "")
    if event["tip"] == "derin":
        baslik = "🔄 DERİN DÖNÜŞ — İZLE"
        sure = "birkaç saatlik düşüş"
    else:
        baslik = "⚡ SAAT İÇİ DÖNÜŞ — İZLE"
        sure = "15–60dk çekiliş"
    return (
        f"{baslik}\n"
        f"{coin}\n"
        f"💰 Fiyat: {event['giris']:.8g}\n"
        f"📉 {sure}: %{float(event.get('cekilis', 0)):.2f}\n"
        f"📈 Dipten dönüş: %{float(event.get('donus', 0)):+.2f}\n"
        f"🟢 3dk: %{float(event.get('d3', 0)):+.2f} | 5dk: %{float(event.get('d5', 0)):+.2f}\n"
        f"🔥 Hacim: {float(event.get('hacim_x', 0)):.2f}x | "
        f"Spread: %{float(event.get('spread', 0)):.2f}\n"
        f"📚 Alış/Satış derinliği: {float(event.get('orderbook', 0)):.2f}x\n"
        f"🌍 BTC 5dk: %{float(event.get('btc5', 0)):+.2f} | "
        f"Piyasa 5dk: %{float(event.get('piyasa5', 0)):+.2f}\n"
        "🧪 Gölge takip: +%1 / +%1.5 / +%2 — gerçek emir yok"
    )


def takip_guncelle(ticker):
    fiyatlar = {}
    for x in ticker:
        try:
            fiyatlar[str(x.get("pair", ""))] = float(x.get("last", 0) or 0)
        except Exception:
            pass
    simdi = time.time()
    degisti = False
    for k in KAYITLAR:
        if k.get("tamamlandi"):
            continue
        fiyat = fiyatlar.get(k.get("symbol"))
        giris = float(k.get("giris", 0) or 0)
        if not fiyat or giris <= 0:
            continue
        getiri = pct(fiyat, giris)
        k["max_getiri"] = round(max(float(k.get("max_getiri", 0) or 0), getiri), 3)
        k["min_getiri"] = round(min(float(k.get("min_getiri", 0) or 0), getiri), 3)
        k["son_getiri"] = round(getiri, 3)
        if simdi - float(k.get("zaman", simdi) or simdi) >= SONUC_SURESI:
            k["tamamlandi"] = True
            k["tamamlanma_zamani"] = simdi
        degisti = True
    if degisti:
        atomik_json_yaz(KAYIT_DOSYA, KAYITLAR)


def oran(grup, hedef):
    return (
        sum(1 for k in grup if float(k.get("max_getiri", 0) or 0) >= hedef)
        / len(grup) * 100 if grup else 0.0
    )


def gunluk_rapor_gerekirse():
    simdi = time.time()
    son = float(META.get("son_rapor", simdi) or simdi)
    if simdi - son < 24 * 60 * 60:
        return
    pencere = [
        k for k in KAYITLAR
        if k.get("tamamlandi") and simdi - float(k.get("zaman", 0) or 0) <= 3 * 24 * 60 * 60
    ]
    satirlar = ["📊 3 GÜNLÜK İKİ DÖNÜŞ GÖLGE RAPORU"]
    for tip, ad in (("saatlik", "⚡ Saat İçi Dönüş"), ("derin", "🔄 Derin Dönüş")):
        g = [k for k in pencere if k.get("tip") == tip]
        yanlis = sum(
            1 for k in g
            if float(k.get("max_getiri", 0) or 0) < 1.5
            and float(k.get("min_getiri", 0) or 0) <= YANLIS_ESIK
        ) / len(g) * 100 if g else 0.0
        satirlar.append(
            f"• {ad}: n={len(g)} | +%1 %{oran(g,1):.1f} | +%1.5 %{oran(g,1.5):.1f} | "
            f"+%2 %{oran(g,2):.1f} | yanlış %{yanlis:.1f}"
        )
    satirlar.append("• Sonuçlar gerçek emir değildir; girişten sonraki 3 saati ölçer.")
    telegram_gonder("\n".join(satirlar))
    META["son_rapor"] = simdi
    atomik_json_yaz(META_DOSYA, META)


def tara():
    ticker = ticker_getir()
    takip_guncelle(ticker)
    secilen = likit_coinleri_sec(ticker)
    symbol_list = [x[1] for x in secilen]
    if "BTCTRY" not in symbol_list:
        symbol_list.append("BTCTRY")

    veriler = {}
    with ThreadPoolExecutor(max_workers=8) as havuz:
        gelecekler = {havuz.submit(coin_verisini_getir, s): s for s in symbol_list}
        for gelecek in as_completed(gelecekler):
            symbol, bir, onbes = gelecek.result()
            veriler[symbol] = (bir, onbes)

    ozetler = {s: temel_1dk_ozet(veriler.get(s, ({}, {}))[0]) for s in symbol_list}
    btc5 = float((ozetler.get("BTCTRY") or {}).get("d5", 0) or 0)
    piyasa5_list = [
        float((ozetler.get(s) or {}).get("d5", 0) or 0)
        for s in symbol_list if s != "BTCTRY" and ozetler.get(s)
    ]
    piyasa5 = statistics.median(piyasa5_list) if piyasa5_list else 0.0
    if btc5 < BTC_5DK_ALT_SINIR or piyasa5 < PIYASA_5DK_ALT_SINIR:
        print(f"Yeni düşüş dalgası: BTC5={btc5:+.2f}, piyasa5={piyasa5:+.2f}; sinyal yok.")
        gunluk_rapor_gerekirse()
        return

    fiyat_haritasi = {s: f for _, s, f, _ in secilen}
    simdi = time.time()
    for _, symbol, ticker_fiyat, ticker_spread in secilen:
        oz = ozetler.get(symbol) or {}
        if not oz or not oz.get("yesil") or not oz.get("teyit"):
            continue
        # Aynı kapanmış mum iki taramada tekrar mesajlanmaz.
        if any(
            k.get("symbol") == symbol and k.get("mum_zamani") == oz.get("mum_zamani")
            for k in KAYITLAR[-200:]
        ):
            continue
        d15 = veriler.get(symbol, ({}, {}))[1]
        derin = derin_ozet(d15, oz)
        derin_aday = bool(
            derin
            and -DERIN_CEKILIS_MAX <= derin.get("cekilis", 0) <= -DERIN_CEKILIS_MIN
            and derin.get("donus", 0) >= DERIN_DONUS_MIN
            and derin.get("kirmizi_pay", 0) >= DERIN_KIRMIZI_PAY_MIN
            and oz.get("hacim_x", 0) >= DERIN_HACIM_MIN
            and oz.get("d3", 0) > 0
        )
        saatlik_aday = bool(
            -SAATLIK_CEKILIS_MAX <= oz.get("cekilis", 0) <= -SAATLIK_CEKILIS_MIN
            and oz.get("donus", 0) >= SAATLIK_DONUS_MIN
            and oz.get("govde", 0) >= SAATLIK_MUM_GOVDE_MIN
            and oz.get("hacim_x", 0) >= SAATLIK_HACIM_MIN
            and oz.get("d3", 0) > 0
        )
        tip = "derin" if derin_aday else ("saatlik" if saatlik_aday else None)
        if not tip:
            continue
        soguma = DERIN_SOGUMA if tip == "derin" else AYNI_TIP_SOGUMA
        if simdi - son_mesaj_zamani(symbol, tip) < soguma:
            continue
        ob = orderbook_ozeti(symbol)
        if not ob or ob.get("spread", 99) > MAX_SPREAD:
            continue
        # Satış duvarı belirginse ilk yeşil mum tek başına yeterli sayılmaz.
        if ob.get("dengesizlik", 0) < 0.70:
            continue
        fiyat = float(ob.get("ask", 0) or fiyat_haritasi.get(symbol, ticker_fiyat))
        event = kayit_ac(symbol, tip, fiyat, oz, derin, ob, btc5, piyasa5)
        telegram_gonder(mesaj_uret(event))

    gunluk_rapor_gerekirse()


def main():
    telegram_gonder(
        "✅ İki Dönüş Gölge Motoru başladı.\n"
        "⚡ Saat İçi Dönüş + 🔄 Derin Dönüş izleniyor.\n"
        "Gerçek emir yok."
    )
    while True:
        bas = time.time()
        try:
            tara()
        except Exception as e:
            print("Genel tarama hatası:", e)
        bekle = max(5, TARAMA_SURESI - (time.time() - bas))
        time.sleep(bekle)


if __name__ == "__main__":
    main()
