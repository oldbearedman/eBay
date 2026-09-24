# eBay Manager

Liest alle aktiven eBay-Angebote aus, zeigt sie auf einer lokalen Seite und
(demnächst) bündelt mehrere Angebote zu einem neuen Kombi-Angebot mit Collage.

Läuft auf dem Wyse (`familienserver`, 192.168.178.33) unter
**http://192.168.178.33:8090** als systemd-Benutzerdienst `ebay-manager`.

## Einrichtung
1. `.env.example` nach `.env` kopieren und die eBay-Schlüssel eintragen
   (developer.ebay.com → Application Keys → Production).
2. Lokal testen: `pip install -r requirements.txt` und `python run.py`
3. Auf den Wyse bringen: `bash deploy/deploy.sh`
4. Im Browser unter „eBay-Zugang" einmalig bei eBay anmelden.

## Dienst auf dem Wyse
    systemctl --user status ebay-manager
    journalctl --user -u ebay-manager -f
