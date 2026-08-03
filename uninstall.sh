#!/usr/bin/env bash
set -euo pipefail

ODM_PURGE_DATA=0
if [[ "${1:-}" == "--purge-data" ]]; then
  ODM_PURGE_DATA=1
  shift
fi
if (($#)); then
  printf 'Aufruf: sudo ./uninstall.sh [--purge-data]\n' >&2
  exit 2
fi
if ((EUID != 0)); then
  printf 'FEHLER: als root ausführen: sudo ./uninstall.sh\n' >&2
  exit 1
fi

systemctl disable --now open-dachs-manager-web.service open-dachs-manager-serial.service 2>/dev/null || true
rm -f -- /etc/systemd/system/open-dachs-manager-web.service
rm -f -- /etc/systemd/system/open-dachs-manager-serial.service
systemctl daemon-reload
rm -f -- /usr/local/bin/open-dachs
rm -f -- /usr/local/bin/open-dachs-manager
rm -rf -- /opt/open-dachs-manager
rm -rf -- /etc/open-dachs-manager

if ((ODM_PURGE_DATA)); then
  rm -rf -- /var/lib/open-dachs-manager
  userdel open-dachs 2>/dev/null || true
  groupdel open-dachs 2>/dev/null || true
  printf 'Open Dachs Manager und alle lokalen Daten wurden entfernt.\n'
else
  printf 'Open Dachs Manager wurde entfernt. Lokale Daten bleiben unter /var/lib/open-dachs-manager erhalten.\n'
fi
