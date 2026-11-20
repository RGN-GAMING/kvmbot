# MINE-CLOUD TCG BOT

**Ek bilkul naya bot — jisme na LXC hai, na /dev/kvm ki zarurat.**

Ye bot full virtual machines banata hai **QEMU TCG (software emulation)** ke through. Aapke VPS par:
- LXC support nahi hai → koi baat nahi
- `/dev/kvm` nahi hai → koi baat nahi
- Nested virtualization enable nahi kar sakte → koi baat nahi

Phir bhi VM banega. Bas performance slow hoga kyunki CPU software se emulate hota hai.

## Kaise kaam karta hai

- `virt-install --virt-type qemu` (kabhi `--virt-type kvm` nahi)
- CPU model: `qemu64`
- Disk: qcow2 cloud image + cloud-init
- Network: libvirt `default` NAT network
- Public SSH: host par `socat` se guest ki port 22 ko random public port par forward kiya jata hai

## Install

```bash
cd tcg-bot
chmod +x install.sh
sudo ./install.sh
```

Installer kuch bhi KVM hardware check nahi karta. Bas QEMU/libvirt/socat aur Python deps install karta hai, token puchta hai, aur `tcgbot.service` start karta hai.

## Commands

| Command | Kaam |
|---|---|
| `!ping` | Bot zinda hai ya nahi |
| `!mode` | Batata hai ki TCG mode chal raha hai |
| `!deploy` | OS chun ke VM banata hai (default 1GB/1vCPU/10GB) |
| `!mylvps` | Aapki VMs |
| `!manage <name>` | Start / Stop / Restart / SSH / Delete buttons |
| `!ssh <name>` | Public SSH command deta hai |
| `!allvps` | Admin: saari VMs |
| `!create <ram> <cpu> <disk> @user` | Admin: user ke liye VM |

## Logs

```bash
journalctl -u tcgbot -f
# ya
tail -f /root/tcg-bot.log
```

## Config (`/root/tcg-bot.env`)

| Variable | Default |
|---|---|
| `IMAGE_DIR` | `/var/lib/libvirt/images/tcg-bot` |
| `LIBVIRT_NETWORK` | `default` |
| `VM_CPU_MODEL` | `qemu64` |
| `DEFAULT_RAM_GB` | `1` |
| `DEFAULT_CPU` | `1` |
| `DEFAULT_DISK_GB` | `10` |
| `PUBLIC_IP` | auto-detect |

## Limit

TCG slow hai. Agar aapko fast VMs chahiye to kabhi bhi provider se `/dev/kvm` enable karwa kar purana KVM bot use kar sakte hain. Is bot ka matlab hi hai: **bina LXC, bina KVM ke chalana**.
