#!/usr/bin/env bash
set -euo pipefail

readonly ODM_INSTALL_ROOT="/opt/open-dachs-manager"
readonly ODM_VENV="$ODM_INSTALL_ROOT/venv"
readonly ODM_CONFIG_DIR="/etc/open-dachs-manager"
readonly ODM_CONFIG_FILE="$ODM_CONFIG_DIR/open-dachs-manager.env"
readonly ODM_DATA_DIR="/var/lib/open-dachs-manager"
readonly ODM_SERVICE_USER="open-dachs"
readonly ODM_SERIAL_SERVICE="open-dachs-manager-serial.service"
readonly ODM_WEB_SERVICE="open-dachs-manager-web.service"
readonly ODM_LEGACY_SERIAL_SERVICE="dachs-v3-serial-worker.service"
readonly ODM_LEGACY_WEB_SERVICE="dachs-v3-web.service"
readonly ODM_LEGACY_DATA_DIR="/var/lib/dachs-v3-web"

ODM_PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ODM_SERIAL_PORT=""
ODM_WEB_HOST="0.0.0.0"
ODM_WEB_PORT="8084"
ODM_BASE_PATH=""
ODM_SERVICE_CODES_FILE=""
ODM_NO_START=0
ODM_REPLACE_LEGACY=0

usage() {
  cat <<'EOF'
Open Dachs Manager – Installation

Aufruf:
  sudo ./install.sh [Optionen]

Optionen:
  --serial-port PFAD  Serielles Gerät; ohne Angabe automatische Erkennung
  --web-host ADRESSE  Bind-Adresse (Standard: 0.0.0.0)
  --web-port PORT     HTTP-Port (Standard: 8084)
  --base-path PFAD    URL-Präfix, zum Beispiel /dachs (Standard: kein Präfix)
  --service-codes-file PFAD
                      Lokale Diagnoseergänzung mit Ursachen/Maßnahmen installieren
  --no-start          Paket und Dienste installieren, aber nicht starten
  --replace-legacy    Alte dachs-v3-Dienste stoppen und lokale Daten übernehmen
  -h, --help          Diese Hilfe anzeigen

Ein erneuter Aufruf nach `git pull` aktualisiert die bestehende Installation.
Auf Debian und Raspberry Pi OS installiert das Skript fehlende Systempakete
(Python, venv, pip, Git und CA-Zertifikate) automatisch über apt.
EOF
}

die() {
  printf 'FEHLER: %s\n' "$*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --serial-port)
      (($# >= 2)) || die "--serial-port benötigt einen Pfad"
      ODM_SERIAL_PORT="$2"
      shift 2
      ;;
    --web-host)
      (($# >= 2)) || die "--web-host benötigt eine Adresse"
      ODM_WEB_HOST="$2"
      shift 2
      ;;
    --web-port)
      (($# >= 2)) || die "--web-port benötigt eine Portnummer"
      ODM_WEB_PORT="$2"
      shift 2
      ;;
    --base-path)
      (($# >= 2)) || die "--base-path benötigt einen URL-Pfad"
      ODM_BASE_PATH="$2"
      shift 2
      ;;
    --service-codes-file)
      (($# >= 2)) || die "--service-codes-file benötigt einen Dateipfad"
      ODM_SERVICE_CODES_FILE="$2"
      shift 2
      ;;
    --no-start)
      ODM_NO_START=1
      shift
      ;;
    --replace-legacy)
      ODM_REPLACE_LEGACY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unbekannte Option: $1"
      ;;
  esac
done

((EUID == 0)) || die "Installer als root ausführen: sudo ./install.sh"
[[ "$ODM_WEB_PORT" =~ ^[0-9]+$ ]] || die "Web-Port muss numerisch sein"
((ODM_WEB_PORT >= 1 && ODM_WEB_PORT <= 65535)) || die "Web-Port muss zwischen 1 und 65535 liegen"
[[ "$ODM_WEB_HOST" != *[[:space:]\"\']* ]] || die "Web-Adresse enthält nicht unterstützte Zeichen"
if [[ -n "$ODM_BASE_PATH" && "$ODM_BASE_PATH" != "/" ]]; then
  [[ "$ODM_BASE_PATH" == /* ]] || die "Base Path muss mit / beginnen"
  [[ "$ODM_BASE_PATH" =~ ^/[A-Za-z0-9._~/-]+$ ]] || die "Base Path enthält nicht unterstützte Zeichen"
  [[ "$ODM_BASE_PATH" != *"//"* && "$ODM_BASE_PATH" != *"/../"* && "$ODM_BASE_PATH" != *"/./"* && "$ODM_BASE_PATH" != */.. && "$ODM_BASE_PATH" != */. ]] || die "Base Path enthält ungültige Segmente"
  while [[ "$ODM_BASE_PATH" == */ ]]; do ODM_BASE_PATH="${ODM_BASE_PATH%/}"; done
else
  ODM_BASE_PATH=""
fi
if [[ -n "$ODM_SERVICE_CODES_FILE" ]]; then
  [[ -f "$ODM_SERVICE_CODES_FILE" && -r "$ODM_SERVICE_CODES_FILE" ]] || die "Fehlerkatalog nicht lesbar: $ODM_SERVICE_CODES_FILE"
fi

install_system_dependencies() {
  local packages=(git python3 python3-venv python3-pip ca-certificates)
  local missing=()
  local package_name

  if command -v apt-get >/dev/null 2>&1 && command -v dpkg-query >/dev/null 2>&1; then
    for package_name in "${packages[@]}"; do
      if ! dpkg-query -W -f='${Status}' "$package_name" 2>/dev/null | grep -q '^install ok installed$'; then
        missing+=("$package_name")
      fi
    done
    if ((${#missing[@]})); then
      printf 'Installiere fehlende Systempakete: %s\n' "${missing[*]}"
      apt-get update
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
    else
      printf 'Systemabhängigkeiten sind bereits installiert.\n'
    fi
    return
  fi

  for package_name in git python3; do
    command -v "$package_name" >/dev/null 2>&1 || missing+=("$package_name")
  done
  if ((${#missing[@]})); then
    die "fehlende Systemabhängigkeiten: ${missing[*]}; automatische Installation wird derzeit für Debian/Raspberry Pi OS unterstützt"
  fi
  python3 -m venv --help >/dev/null 2>&1 || die "Python-venv fehlt; bitte das passende python3-venv-Paket installieren"
}

install_system_dependencies

for command_name in python3 systemctl getent groupadd useradd usermod install stat mktemp tar readlink chmod chown; do
  command -v "$command_name" >/dev/null 2>&1 || die "benötigter Befehl fehlt: $command_name"
done

python3 - <<'PY' || die "Python 3.11 oder neuer wird benötigt"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY

if [[ -z "$ODM_SERIAL_PORT" && -d /dev/serial/by-id ]]; then
  while IFS= read -r candidate; do
    ODM_SERIAL_PORT="$candidate"
    break
  done < <(find /dev/serial/by-id -maxdepth 1 -type l -print | sort)
fi
if [[ -z "$ODM_SERIAL_PORT" && -e /dev/ttyUSB0 ]]; then
  ODM_SERIAL_PORT="/dev/ttyUSB0"
fi
if [[ -z "$ODM_SERIAL_PORT" ]]; then
  ODM_SERIAL_PORT="/dev/ttyUSB0"
fi
[[ "$ODM_SERIAL_PORT" != *[[:space:]\"\']* ]] || die "serieller Gerätepfad enthält nicht unterstützte Zeichen"

if ((ODM_NO_START == 0)) && [[ ! -e "$ODM_SERIAL_PORT" ]]; then
  die "serielles Gerät nicht gefunden: $ODM_SERIAL_PORT (anschließen, --serial-port angeben oder --no-start verwenden)"
fi

if ((ODM_NO_START == 0 && ODM_REPLACE_LEGACY == 0)); then
  if systemctl is-active --quiet "$ODM_LEGACY_SERIAL_SERVICE" || systemctl is-active --quiet "$ODM_LEGACY_WEB_SERVICE"; then
    die "alte dachs-v3-Dienste sind aktiv; zur Migration erneut mit --replace-legacy ausführen"
  fi
fi

if ! getent group "$ODM_SERVICE_USER" >/dev/null; then
  groupadd --system "$ODM_SERVICE_USER"
fi
if ! getent passwd "$ODM_SERVICE_USER" >/dev/null; then
  ODM_NOLOGIN_SHELL="/usr/sbin/nologin"
  [[ -x "$ODM_NOLOGIN_SHELL" ]] || ODM_NOLOGIN_SHELL="/bin/false"
  useradd --system --gid "$ODM_SERVICE_USER" --home-dir "$ODM_DATA_DIR" --shell "$ODM_NOLOGIN_SHELL" "$ODM_SERVICE_USER"
fi

ODM_CALLER="${SUDO_USER:-}"
if [[ -n "$ODM_CALLER" && "$ODM_CALLER" != "root" ]] && getent passwd "$ODM_CALLER" >/dev/null; then
  usermod -a -G "$ODM_SERVICE_USER" "$ODM_CALLER"
fi

for device_group in dialout plugdev; do
  if getent group "$device_group" >/dev/null; then
    usermod -a -G "$device_group" "$ODM_SERVICE_USER"
  fi
done
if [[ -e "$ODM_SERIAL_PORT" ]]; then
  ODM_DEVICE_GROUP="$(stat -Lc '%G' "$ODM_SERIAL_PORT")"
  if [[ "$ODM_DEVICE_GROUP" != "UNKNOWN" ]] && getent group "$ODM_DEVICE_GROUP" >/dev/null; then
    usermod -a -G "$ODM_DEVICE_GROUP" "$ODM_SERVICE_USER"
  fi
fi

install -d -m 0755 -o root -g root "$ODM_INSTALL_ROOT"
install -d -m 0750 -o "$ODM_SERVICE_USER" -g "$ODM_SERVICE_USER" "$ODM_DATA_DIR"
install -d -m 0750 -o root -g "$ODM_SERVICE_USER" "$ODM_CONFIG_DIR"

ODM_INSTALLED_SERVICE_CODES="$ODM_DATA_DIR/servicecodes_de.properties"
if [[ -n "$ODM_SERVICE_CODES_FILE" ]]; then
  if [[ "$(readlink -f -- "$ODM_SERVICE_CODES_FILE")" != "$(readlink -m -- "$ODM_INSTALLED_SERVICE_CODES")" ]]; then
    install -m 0640 -o "$ODM_SERVICE_USER" -g "$ODM_SERVICE_USER" "$ODM_SERVICE_CODES_FILE" "$ODM_INSTALLED_SERVICE_CODES"
  else
    chown "$ODM_SERVICE_USER:$ODM_SERVICE_USER" "$ODM_INSTALLED_SERVICE_CODES"
    chmod 0640 "$ODM_INSTALLED_SERVICE_CODES"
  fi
  printf 'Lokaler Fehlerkatalog installiert: %s\n' "$ODM_INSTALLED_SERVICE_CODES"
fi

python3 -m venv "$ODM_VENV" || die "Python-Umgebung konnte nicht erstellt werden"

# Build from a fresh staging tree.  Setuptools may otherwise reuse an ignored
# build/ directory from an older checkout and accidentally retain files that
# were deliberately removed from a newer release.
ODM_BUILD_SOURCE="$(mktemp -d /tmp/open-dachs-manager-install.XXXXXX)"
tar \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='build' \
  --exclude='*.egg-info' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  -C "$ODM_PROJECT_DIR" -cf - . | tar -C "$ODM_BUILD_SOURCE" -xf -
"$ODM_VENV/bin/python" -m pip \
  --disable-pip-version-check \
  --no-cache-dir \
  install --upgrade "$ODM_BUILD_SOURCE"
rm -rf -- "$ODM_BUILD_SOURCE"
ln -sfn "$ODM_VENV/bin/open-dachs" /usr/local/bin/open-dachs
ln -sfn "$ODM_VENV/bin/open-dachs-manager" /usr/local/bin/open-dachs-manager

ODM_CONFIG_TEMP="$(mktemp "$ODM_CONFIG_DIR/.open-dachs-manager.env.XXXXXX")"
trap 'rm -f -- "$ODM_CONFIG_TEMP"' EXIT
{
  printf 'OPEN_DACHS_SERIAL_PORT=%s\n' "$ODM_SERIAL_PORT"
  printf 'OPEN_DACHS_SERIAL_SOCKET=/run/open-dachs-manager/serial.sock\n'
  printf 'OPEN_DACHS_BAUD=19200\n'
  printf 'OPEN_DACHS_WEB_HOST=%s\n' "$ODM_WEB_HOST"
  printf 'OPEN_DACHS_WEB_PORT=%s\n' "$ODM_WEB_PORT"
  printf 'OPEN_DACHS_BASE_PATH=%s\n' "$ODM_BASE_PATH"
  printf 'OPEN_DACHS_WEB_DATA_DIR=%s\n' "$ODM_DATA_DIR"
  if [[ -f "$ODM_INSTALLED_SERVICE_CODES" ]]; then
    printf 'OPEN_DACHS_SERVICE_CODES_FILE=%s\n' "$ODM_INSTALLED_SERVICE_CODES"
  else
    printf 'OPEN_DACHS_SERVICE_CODES_FILE=\n'
  fi
  printf 'OPEN_DACHS_TIMEOUT=0.9\n'
  printf 'OPEN_DACHS_WEB_INTERVAL=0.75\n'
  printf 'OPEN_DACHS_MAINTENANCE_LIVE_WRITES=0\n'
} >"$ODM_CONFIG_TEMP"
install -m 0640 -o root -g "$ODM_SERVICE_USER" "$ODM_CONFIG_TEMP" "$ODM_CONFIG_FILE"

install -m 0644 "$ODM_PROJECT_DIR/systemd/$ODM_SERIAL_SERVICE" "/etc/systemd/system/$ODM_SERIAL_SERVICE"
install -m 0644 "$ODM_PROJECT_DIR/systemd/$ODM_WEB_SERVICE" "/etc/systemd/system/$ODM_WEB_SERVICE"
systemctl daemon-reload

if ((ODM_NO_START)); then
  printf '\nOpen Dachs Manager wurde installiert, die Dienste wurden nicht gestartet.\n'
  printf 'Später starten mit: systemctl enable --now %s %s\n' "$ODM_SERIAL_SERVICE" "$ODM_WEB_SERVICE"
  if [[ -n "$ODM_CALLER" && "$ODM_CALLER" != "root" ]]; then
    printf 'Einmal ab- und wieder anmelden, damit %s auf den Worker-Socket zugreifen kann.\n' "$ODM_CALLER"
  fi
  exit 0
fi

systemctl stop "$ODM_WEB_SERVICE" "$ODM_SERIAL_SERVICE" 2>/dev/null || true

if ((ODM_REPLACE_LEGACY)); then
  systemctl stop "$ODM_LEGACY_WEB_SERVICE" "$ODM_LEGACY_SERIAL_SERVICE" 2>/dev/null || true
  systemctl disable "$ODM_LEGACY_WEB_SERVICE" "$ODM_LEGACY_SERIAL_SERVICE" 2>/dev/null || true
  if [[ -d "$ODM_LEGACY_DATA_DIR" ]] && [[ -z "$(find "$ODM_DATA_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    cp -a "$ODM_LEGACY_DATA_DIR/." "$ODM_DATA_DIR/"
    chown -R "$ODM_SERVICE_USER:$ODM_SERVICE_USER" "$ODM_DATA_DIR"
    printf 'Lokale Webdaten aus %s wurden übernommen.\n' "$ODM_LEGACY_DATA_DIR"
  fi
fi

ODM_RESOLVED_SERIAL="$ODM_SERIAL_PORT"
if [[ -e "$ODM_SERIAL_PORT" ]]; then
  ODM_RESOLVED_SERIAL="$(readlink -f -- "$ODM_SERIAL_PORT")"
fi
if command -v fuser >/dev/null 2>&1 && fuser "$ODM_RESOLVED_SERIAL" >/dev/null 2>&1; then
  fuser -v "$ODM_RESOLVED_SERIAL" >&2 || true
  die "das serielle Gerät wird noch von einem anderen Prozess verwendet"
fi

systemctl enable "$ODM_SERIAL_SERVICE" "$ODM_WEB_SERVICE" >/dev/null
if ! systemctl start "$ODM_SERIAL_SERVICE"; then
  journalctl -u "$ODM_SERIAL_SERVICE" -n 30 --no-pager >&2 || true
  die "Serialworker konnte nicht gestartet werden"
fi
ODM_FRESH_USERS=0
[[ -f "$ODM_DATA_DIR/users.json" ]] || ODM_FRESH_USERS=1
if ! systemctl start "$ODM_WEB_SERVICE"; then
  journalctl -u "$ODM_WEB_SERVICE" -n 30 --no-pager >&2 || true
  die "Webdienst konnte nicht gestartet werden"
fi

systemctl is-active --quiet "$ODM_SERIAL_SERVICE" || die "Serialworker ist nicht aktiv"
systemctl is-active --quiet "$ODM_WEB_SERVICE" || die "Webdienst ist nicht aktiv"

printf '\nOpen Dachs Manager ist installiert.\n'
printf 'Web: http://%s:%s%s\n' "$ODM_WEB_HOST" "$ODM_WEB_PORT" "${ODM_BASE_PATH:-/}"
printf 'CLI: %s/bin/open-dachs doctor\n' "$ODM_VENV"
printf 'Dienste: %s, %s\n' "$ODM_SERIAL_SERVICE" "$ODM_WEB_SERVICE"
if [[ -n "$ODM_CALLER" && "$ODM_CALLER" != "root" ]]; then
printf 'Einmal ab- und wieder anmelden, damit %s auf den Worker-Socket zugreifen kann.\n' "$ODM_CALLER"
fi
if ((ODM_FRESH_USERS)); then
  printf '\nErstzugang (wird nur einmal angezeigt):\n'
  journalctl -u "$ODM_WEB_SERVICE" -n 30 --no-pager 2>/dev/null | sed -n '/Web-Erstzugang/,+2p' || true
fi
