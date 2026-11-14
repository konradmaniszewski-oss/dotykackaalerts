import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone

# ==========================================
# 1. KONFIGURACJA - wklej tokeny między cudzysłowy (bez os.environ.get!)
# ==========================================
DOTYPOS_REFRESH_TOKEN = os.environ.get("DOTYPOS_REFRESH_TOKEN", "WKLEJ_REFRESH_TOKEN")
DOTYPOS_CLOUD_ID = 323193401

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "WKLEJ_TOKEN_BOTA")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-5584204646")

# Progi alarmowe
PROG_RABATU_PROCENT = 20.0   # alert gdy rabat > 20%
PROG_WYPLATY_KW = 100.0      # alert gdy wypłata z kasy > 100 zł
OKNO_GODZIN = int(os.environ.get("OKNO_GODZIN", "24"))             # przy odpalaniu co kilka minut wystarczy 2
LIMIT_STRONY = 100
MAX_STRON = 20
DEBUG = False                # True = wypisz przykładowe rekordy i typy operacji kasowych

DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)
PLIK_HISTORII = os.path.join(DATA_DIR, "wyslane_alerty.json")
PLIK_CACHE = os.path.join(DATA_DIR, "pozycje_cache.json")
CACHE_DNI = 7


def wczytaj_historie():
    if os.path.exists(PLIK_HISTORII):
        try:
            with open(PLIK_HISTORII, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def zapisz_historie(historia):
    with open(PLIK_HISTORII, "w", encoding="utf-8") as f:
        json.dump(sorted(historia), f)


def wczytaj_cache():
    """Ostatnia znana ilość aktywnych pozycji (po anulowaniu API zeruje ilość)."""
    if os.path.exists(PLIK_CACHE):
        try:
            with open(PLIK_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def zapisz_cache(cache):
    granica = (datetime.now(timezone.utc) - timedelta(days=CACHE_DNI)).isoformat()
    swieze = {k: v for k, v in cache.items() if v.get("t", "") >= granica}
    with open(PLIK_CACHE, "w", encoding="utf-8") as f:
        json.dump(swieze, f)


def wyslij_telegram(wiadomosc):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN.strip()}/sendMessage"
    dane = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": wiadomosc,
        "parse_mode": "HTML"
    }).encode("utf-8")
    req = urllib.request.Request(url, data=dane)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()


def wyslij_i_zapamietaj(wiadomosc, klucz, historia):
    """Wysyła alert; klucz trafia do historii tylko gdy wysyłka się udała."""
    try:
        wyslij_telegram(wiadomosc)
        historia.add(klucz)
    except Exception as e:
        print(f"Nie udało się wysłać alertu {klucz}: {e}")


def pobierz_access_token():
    url = "https://api.dotykacka.cz/v2/signin/token"
    headers = {
        "Authorization": f"User {DOTYPOS_REFRESH_TOKEN.strip()}",
        "Content-Type": "application/json"
    }
    body = json.dumps({"_cloudId": DOTYPOS_CLOUD_ID}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))["accessToken"]
    except urllib.error.HTTPError as e:
        tresc = e.read().decode("utf-8", errors="ignore")
        print(f"Błąd logowania do Dotypos ({e.code}): {tresc}")
        raise


def dotypos_get(endpoint, access_token, params=None, ignoruj_404=False):
    url = f"https://api.dotykacka.cz/v2/clouds/{DOTYPOS_CLOUD_ID}/{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404 and ignoruj_404:
            return {"data": []}  # API zwraca 404, gdy filtr nic nie znalazł
        tresc = e.read().decode("utf-8", errors="ignore")
        print(f"Błąd API na {endpoint} ({e.code}): {tresc}")
        raise


def pobierz_ostatnie(endpoint, access_token):
    """
    Rekordy z ostatnich OKNO_GODZIN godzin. Najpierw filtr po versionDate
    (łapie też stare rekordy zmienione dopiero co, np. anulowane pozycje),
    potem po created, na końcu jedna strona bez filtra.
    """
    od = (datetime.now(timezone.utc) - timedelta(hours=OKNO_GODZIN)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    for pole in ("versionDate", "created"):
        try:
            wyniki = []
            for strona in range(1, MAX_STRON + 1):
                odp = dotypos_get(endpoint, access_token, {
                    "limit": LIMIT_STRONY,
                    "page": strona,
                    "filter": f"{pole}|gteq|{od}",
                    "sort": pole
                }, ignoruj_404=True)
                dane = odp.get("data", [])
                wyniki.extend(dane)
                if len(dane) < LIMIT_STRONY:
                    break
            return wyniki
        except urllib.error.HTTPError:
            print(f"[{endpoint}] filtr po '{pole}' nie zadziałał, próbuję dalej.")
    odp = dotypos_get(endpoint, access_token, {"limit": LIMIT_STRONY})
    return odp.get("data", [])


def czy_swieze(rekord):
    data_str = rekord.get("versionDate") or rekord.get("created")
    if not data_str:
        return True
    try:
        dt = datetime.fromisoformat(str(data_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= (datetime.now(timezone.utc) - timedelta(hours=OKNO_GODZIN))
    except Exception:
        return True


def liczba(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def podglad(nazwa, dane):
    if DEBUG:
        print(f"--- {nazwa}: {len(dane)} rekordów ---")
        if dane:
            print(json.dumps(dane[0], indent=2, ensure_ascii=False))


def sprawdz_restauracje():
    historia = wczytaj_historie()
    cache = wczytaj_cache()
    try:
        token = pobierz_access_token()

        # 1. Mapa pracowników
        pracownicy = {}
        try:
            for e in dotypos_get("employees", token, {"limit": 100}).get("data", []):
                pracownicy[str(e.get("id"))] = e.get("name") or e.get("fullName") or "Nieznany"
        except Exception:
            pass

        def kto(rekord):
            return pracownicy.get(str(rekord.get("_employeeId")), "Brak przypisania")

        # 2. Rachunki
        orders = pobierz_ostatnie("orders", token)
        podglad("orders", orders)

        for order in orders:
            if not czy_swieze(order):
                continue

            oid = str(order.get("id"))
            kwota = liczba(order.get("totalValueRounded") or order.get("totalValueWithVat"))
            flags = int(liczba(order.get("flags")))
            status = str(order.get("status") or "").lower()
            czy_anulowany = (bool(order.get("canceledDate")) or (flags & 1 != 0)
                             or status in ("canceled", "cancelled", "storno"))

            # REGUŁA A: anulowany cały rachunek / zwrot ujemny
            if czy_anulowany and f"order_cancel_{oid}" not in historia:
                msg = (
                    f"🚨 <b>ANULOWANY RACHUNEK (STORNO)</b>\n"
                    f"• Rachunek ID: <code>{oid}</code>\n"
                    f"• Kwota: <b>{kwota:.2f} zł</b>\n"
                    f"• Pracownik: {kto(order)}"
                )
                wyslij_i_zapamietaj(msg, f"order_cancel_{oid}", historia)

            elif kwota < 0 and f"order_refund_{oid}" not in historia:
                msg = (
                    f"💸 <b>ZWROT GOTÓWKOWY / KOREKTA</b>\n"
                    f"• Rachunek ID: <code>{oid}</code>\n"
                    f"• Kwota zwrotu: <b>{kwota:.2f} zł</b>\n"
                    f"• Pracownik: {kto(order)}"
                )
                wyslij_i_zapamietaj(msg, f"order_refund_{oid}", historia)

        # 3. Pozycje na rachunkach
        items = pobierz_ostatnie("order-items", token)
        podglad("order-items", items)

        for item in items:
            if not czy_swieze(item):
                continue

            iid = str(item.get("id"))
            nazwa = item.get("name") or "Nieznana pozycja"
            ilosc = liczba(item.get("quantity"))
            cena_jedn = liczba(item.get("unitPriceWithVat"))
            wartosc = liczba(item.get("totalPriceWithVat"))
            rachunek = item.get("_orderId")
            czy_usunieto = bool(item.get("canceledDate"))

            # zapamiętaj ilość aktywnej pozycji (po storno API ją zeruje)
            if not czy_usunieto and ilosc > 0:
                cache[iid] = {"q": ilosc, "t": datetime.now(timezone.utc).isoformat()}

            # REGUŁA B: storno (canceledDate) lub zwrot (ilość < 0) pozycji
            if (czy_usunieto or ilosc < 0) and f"item_void_{iid}" not in historia:
                typ = "ZWROT POZYCJI" if ilosc < 0 else "STORNO POZYCJI"
                ile = abs(ilosc) if ilosc < 0 else liczba(cache.get(iid, {}).get("q"))
                if ile > 0:
                    opis = (
                        f"• Pozycja: <b>{nazwa}</b> (x{ile:g})\n"
                        f"• Wartość: <b>{ile * cena_jedn:.2f} zł</b>\n"
                    )
                else:
                    opis = (
                        f"• Pozycja: <b>{nazwa}</b> (ilość nieznana)\n"
                        f"• Cena jednostkowa: <b>{cena_jedn:.2f} zł</b>\n"
                    )
                msg = (
                    f"⚠️ <b>{typ}</b>\n"
                    f"{opis}"
                    f"• Rachunek ID: <code>{rachunek}</code>\n"
                    f"• Pracownik: {kto(item)}"
                )
                wyslij_i_zapamietaj(msg, f"item_void_{iid}", historia)

            # REGUŁA C: wysoki rabat (pole discountPercent)
            rabat = liczba(item.get("discountPercent"))
            if not czy_usunieto and ilosc > 0 and rabat > PROG_RABATU_PROCENT \
                    and f"item_disc_{iid}" not in historia:
                msg = (
                    f"🏷️ <b>WYSOKI RABAT ({rabat:.1f}%)</b>\n"
                    f"• Pozycja: <b>{nazwa}</b> (x{ilosc:g})\n"
                    f"• Wartość po rabacie: <b>{wartosc:.2f} zł</b>\n"
                    f"• Pracownik: {kto(item)}"
                )
                wyslij_i_zapamietaj(msg, f"item_disc_{iid}", historia)

        # 4. Operacje kasowe (wypłaty)
        try:
            logs = pobierz_ostatnie("money-logs", token)
            podglad("money-logs", logs)
            if DEBUG:
                print("Typy operacji kasowych:", sorted({str(l.get('transactionType')) for l in logs}))

            for log in logs:
                if not czy_swieze(log):
                    continue
                typ = str(log.get("transactionType") or "")
                if typ in ("REGISTER_OPEN", "REGISTER_CLOSE"):
                    continue

                lid = str(log.get("id"))
                kwota_log = liczba(log.get("amount"))
                powod = log.get("note") or "Brak opisu"
                wyplata = kwota_log < 0 or any(
                    s in typ.upper() for s in ("WITHDRAW", "CASH_OUT", "PAYOUT", "EXPENSE"))

                # REGUŁA D: wypłata z kasy powyżej progu
                if wyplata and abs(kwota_log) > PROG_WYPLATY_KW \
                        and f"cash_out_{lid}" not in historia:
                    msg = (
                        f"💰 <b>WYPŁATA Z KASY (KW)</b>\n"
                        f"• Kwota: <b>{abs(kwota_log):.2f} zł</b>\n"
                        f"• Typ: {typ}\n"
                        f"• Powód: {powod}\n"
                        f"• Pracownik: {kto(log)}"
                    )
                    wyslij_i_zapamietaj(msg, f"cash_out_{lid}", historia)
        except Exception as e:
            print(f"Nie udało się sprawdzić money-logs: {e}")

    finally:
        zapisz_historie(historia)
        zapisz_cache(cache)


if __name__ == "__main__":
    # INTERWAL_SEKUND > 0 = działaj w pętli (tryb serwerowy); 0 = jedno uruchomienie
    interwal = int(os.environ.get("INTERWAL_SEKUND", "0"))
    if interwal > 0:
        print(f"Start: sprawdzanie co {interwal} s", flush=True)
        while True:
            try:
                sprawdz_restauracje()
            except Exception as e:
                print(f"Błąd w przebiegu: {e}", flush=True)
            time.sleep(interwal)
    else:
        sprawdz_restauracje()
