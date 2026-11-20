#!/bin/bash
# MINE-CLOUD TCG BOT installer
# Full VMs with QEMU TCG. NO LXC, NO /dev/kvm, NO nested virtualization needed.
set -e

RED='\033[0;31m'; GRN='\033[0;32m'; YEL='\033[1;33m'; CYN='\033[0;36m'; NC='\033[0m'
step() { echo -e "${GRN}[+]${NC} $1"; }
warn() { echo -e "${YEL}[!]${NC} $1"; }
err()  { echo -e "${RED}[x]${NC} $1"; }

if [ "$EUID" -ne 0 ]; then
    err "Run as root: sudo ./install.sh"
    exit 1
fi

echo -e "${CYN}MINE-CLOUD TCG BOT — install without LXC and without /dev/kvm${NC}"

export DEBIAN_FRONTEND=noninteractive
step "Updating system..."
apt-get update -qq
apt-get upgrade -y -qq

step "Installing QEMU/libvirt/TCG packages (no LXC, no KVM hardware)..."
apt-get install -y --no-install-recommends \
    qemu-system-x86 qemu-utils \
    libvirt-daemon-system libvirt-clients \
    virtinst libosinfo-bin genisoimage \
    socat curl ca-certificates wget git \
    python3 python3-pip python3-venv python3-dev build-essential \
    iproute2 iputils-ping procps net-tools dnsutils netcat-openbsd

step "Starting libvirt and default NAT network..."
if [ -d /run/systemd/system ]; then
    systemctl enable --now libvirtd || true
else
    command -v libvirtd >/dev/null 2>&1 && libvirtd -d >/dev/null 2>&1 || true
fi
virsh net-autostart default >/dev/null 2>&1 || true
virsh net-start default >/dev/null 2>&1 || true
virsh -c qemu:///system list --all

step "Creating Python virtual environment..."
python3 -m venv /root/tcg-venv
source /root/tcg-venv/bin/activate
python3 -m pip install --upgrade pip -q
python3 -m pip install -q -U discord.py python-dotenv requests
deactivate

step "Installing bot files..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install -m 600 "$SCRIPT_DIR/bot.py" /root/tcg-bot.py
/root/tcg-venv/bin/python -m py_compile /root/tcg-bot.py
mkdir -p /var/lib/libvirt/images/tcg-bot

ENV_FILE=/root/tcg-bot.env
touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

read -rsp "$(echo -e "${CYN}Discord bot token: ${NC}")" TOKEN_IN; echo
[ -z "$TOKEN_IN" ] && { err "Token cannot be empty"; exit 1; }

while true; do
    read -rp "$(echo -e "${CYN}Admin user ID: ${NC}")" ADMIN_IN
    [[ "$ADMIN_IN" =~ ^[0-9]{15,22}$ ]] && break
    warn "Enter numeric Discord user ID."
done

while true; do
    read -rp "$(echo -e "${CYN}VPS role ID: ${NC}")" ROLE_IN
    [[ "$ROLE_IN" =~ ^[0-9]{15,22}$ ]] && break
    warn "Enter numeric Discord role ID."
done

read -rp "$(echo -e "${CYN}Command prefix [!]: ${NC}")" PREFIX_IN
PREFIX_IN="${PREFIX_IN:-!}"

read -rp "$(echo -e "${CYN}Public IP of this VPS (Enter to auto-detect): ${NC}")" PUBIP_IN

TMP=$(mktemp)
grep -vE '^(DISCORD_TOKEN|MAIN_ADMIN_ID|VPS_USER_ROLE_ID|BOT_PREFIX|PUBLIC_IP)=' "$ENV_FILE" > "$TMP" || true
{
    printf 'DISCORD_TOKEN=%s\n' "$TOKEN_IN"
    printf 'MAIN_ADMIN_ID=%s\n' "$ADMIN_IN"
    printf 'VPS_USER_ROLE_ID=%s\n' "$ROLE_IN"
    printf 'BOT_PREFIX=%s\n' "$PREFIX_IN"
    printf 'PUBLIC_IP=%s\n' "$PUBIP_IN"
    cat <<'CFG'
IMAGE_DIR=/var/lib/libvirt/images/tcg-bot
LIBVIRT_NETWORK=default
VM_CPU_MODEL=qemu64
DEFAULT_RAM_GB=1
DEFAULT_CPU=1
DEFAULT_DISK_GB=10
VPS_EXPIRY_DAYS=30
TCG_BOT_DB=/root/tcg-bot.db
CFG
} >> "$TMP"
install -m 600 "$TMP" "$ENV_FILE"
rm -f "$TMP"
unset TOKEN_IN

step "Creating systemd service..."
cat > /etc/systemd/system/tcgbot.service <<EOF
[Unit]
Description=MINE-CLOUD TCG VPS Bot (no LXC / no KVM)
Wants=network-online.target libvirtd.service
After=network-online.target libvirtd.service

[Service]
User=root
WorkingDirectory=/root
EnvironmentFile=-/root/tcg-bot.env
ExecStart=/root/tcg-venv/bin/python /root/tcg-bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

step "Starting bot..."
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload
    systemctl enable tcgbot
    systemctl restart tcgbot
    sleep 3
    systemctl is-active --quiet tcgbot || { journalctl -u tcgbot -n 40 --no-pager; exit 1; }
else
    set -a
    . "$ENV_FILE"
    set +a
    nohup /root/tcg-venv/bin/python /root/tcg-bot.py >/root/tcg-bot.log 2>&1 &
    echo $! >/root/tcg-bot.pid
fi

echo
step "Install complete."
echo -e "  Logs (systemd): ${CYN}journalctl -u tcgbot -f${NC}"
echo -e "  Logs (no systemd): ${CYN}tail -f /root/tcg-bot.log${NC}"
echo -e "  VMs: ${CYN}virsh list --all${NC}"
echo -e "  Images: ${CYN}/var/lib/libvirt/images/tcg-bot${NC}"
echo -e "${YEL}Mode: QEMU TCG software emulation. LXC nahi, /dev/kvm nahi, phir bhi VM banega (slow).${NC}"
