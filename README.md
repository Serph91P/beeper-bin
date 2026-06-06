# Beeper v4 AUR auto updater

Dieses Repo aktualisiert ein AUR Binary-Paket fuer Beeper v4 automatisch ueber GitHub Actions.

Standard ist ein eigenes Paket:

```text
beeper-bin
```

Das existierende Paket `beeper-v4-bin` gehoert aktuell einem anderen Maintainer. Du kannst `beeper-v4-bin` nur direkt publizieren, wenn du im AUR dafuer Maintainer oder Co-Maintainer bist. Ohne diesen Zugriff solltest du das eigene Paket `beeper-bin` verwenden oder den Namen ueber `AUR_PACKAGE_NAME` auf einen freien AUR-Namen setzen.

## Was der Workflow macht

Taeglich um 03:00 UTC und manuell per `workflow_dispatch`:

1. Beeper Update-Feed fuer die neueste Linux-x64 Nightly-Version abfragen.
2. Version und Asset-URL aus dem Update-Feed lesen, zum Beispiel `Beeper-Nightly-4.2.908-x86_64.AppImage`.
3. SHA256 ueber das AppImage berechnen.
4. `PKGBUILD` aktualisieren.
5. `.SRCINFO` mit `makepkg --printsrcinfo` aktualisieren.
6. Als unprivilegierter Arch-User bauen und pruefen.
7. Update in dieses GitHub Repo committen.
8. Optional per SSH in das AUR Git Repo pushen.

## GitHub Secrets

Fuer automatisches AUR-Publishing brauchst du diese Repo-Secrets:

### `AUR_SSH_KEY`

Private SSH Key, dessen Public Key im AUR Account hinterlegt ist.

Der Key muss Schreibzugriff auf das Zielpaket haben. Fuer `beeper-v4-bin` klappt das nur mit Maintainer- oder Co-Maintainer-Rechten.

### `AUR_SSH_KNOWN_HOSTS`

Pinned `known_hosts` Inhalt fuer `aur.archlinux.org`. Beispiel lokal erzeugen und vor dem Eintragen kontrollieren:

```bash
ssh-keyscan aur.archlinux.org
```

Nicht blind kopieren, Host-Key gegen eine vertrauenswuerdige Quelle pruefen. Der Workflow verweigert AUR-Pushes ohne `AUR_SSH_KNOWN_HOSTS`, damit kein `StrictHostKeyChecking=no` verwendet wird.

## Optionale GitHub Variables

- `AUR_PACKAGE_NAME`, Default `beeper-bin`
- `UPSTREAM_QUERY_URL`, Default `https://api.beeper.com/desktop/update-feed.json?bundleID=com.automattic.beeper.desktop&version=0.0.1&platform=linux&arch=x64&channel=nightly`
- `UPSTREAM_VERSION_REGEX`, Default `Beeper-(?P<version>[0-9]+\.[0-9]+\.[0-9]+)`

## Manuell testen

Ohne Dateien zu schreiben:

```bash
python3 scripts/aur_update.py --json --dry-run --package-name beeper-bin
```

Unit-Tests:

```bash
python3 -m unittest discover -s tests -v
```

Auf einem Arch-System oder im Workflow:

```bash
cd packages/beeper-bin
bash -n PKGBUILD
makepkg --printsrcinfo > .SRCINFO
makepkg -s --noconfirm --needed --check
namcap PKGBUILD || true
namcap ./*.pkg.tar.* || true
```

## Paketnamen

Empfohlen, wenn du das bestehende AUR-Paket nicht uebernehmen kannst:

```text
beeper-bin
```

Wenn du spaeter Maintainerzugriff auf `beeper-v4-bin` bekommst, kannst du den Workflow manuell oder dauerhaft mit diesem Namen ausfuehren:

```text
AUR_PACKAGE_NAME=beeper-v4-bin
```

Dann muss auch `packages/beeper-v4-bin/` gepflegt werden und dein AUR SSH Key muss auf dieses AUR Repo pushen duerfen.

## Aktuelle Initialversion

Initialisiert und aktualisiert auf Beeper Nightly `4.2.908` und SHA256:

```text
b2aa7d8dd98df3cdf0d9f3bdb8f9888e929cc25221705ad2c23b245bb9d53d3b
```

Quelle:

```text
https://api.beeper.com/desktop/update-feed.json?bundleID=com.automattic.beeper.desktop&version=0.0.1&platform=linux&arch=x64&channel=nightly
```
